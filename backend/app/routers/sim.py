from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Course, Driver, Enrollment, TrainingSession, TrainingTarget
from app.schemas.sim import (
    AdvanceRequest, AdvanceSummary, CourseProgress, ResetRequest, SimStateOut, SimTotals,
)
from app.services.sim_clock import get_sim_now
from app.llm.planner import active_run_exists
from app.services.errors import Conflict
from app.simulator import engine as sim_engine
from app.simulator.rules import SIM_END, SIM_START, SIM_YEAR
from app.simulator.seed import reseed

router = APIRouter(prefix="/sim", tags=["simulation"])


def _block_while_planning(db: Session) -> None:
    """The clock and the data must not move while the agent is working from a snapshot of "now"."""
    if active_run_exists(db):
        raise Conflict("A planner run is in progress")


def _count(db: Session, model, *conditions) -> int:
    return db.scalar(select(func.count()).select_from(model).where(*conditions)) or 0


def _build_state(db: Session) -> SimStateOut:
    now = get_sim_now(db)
    year_seconds = (SIM_END - SIM_START).total_seconds()
    try:
        seed = sim_engine.get_seed(db)
    except RuntimeError:
        seed = None
    return SimStateOut(
        current_time=now,
        year_start=SIM_START,
        year_end=SIM_END,
        percent_year_elapsed=round(100 * (now - SIM_START).total_seconds() / year_seconds, 1),
        year_ended=now >= SIM_END,
        seed=seed,
        totals=SimTotals(
            drivers=_count(db, Driver),
            active_drivers=_count(db, Driver, Driver.is_active.is_(True)),
            sessions_scheduled=_count(db, TrainingSession, TrainingSession.status == "scheduled"),
            sessions_completed=_count(db, TrainingSession, TrainingSession.status == "completed"),
            sessions_cancelled=_count(db, TrainingSession, TrainingSession.status == "cancelled"),
            enrollments_booked=_count(db, Enrollment, Enrollment.status == "booked"),
            enrollments_attended=_count(db, Enrollment, Enrollment.status == "attended"),
            enrollments_no_show=_count(db, Enrollment, Enrollment.status == "no_show"),
            enrollments_cancelled=_count(db, Enrollment, Enrollment.status == "cancelled"),
        ),
    )


@router.get("/state", response_model=SimStateOut)
def get_state(db: Session = Depends(get_db)):
    """Current simulated time, how much of the year has passed, and quick totals."""
    return _build_state(db)


@router.post("/advance", response_model=AdvanceSummary)
def advance_clock(body: AdvanceRequest, db: Session = Depends(get_db)):
    """Play the simulation forward by ``days`` days (stops at year end)."""
    _block_while_planning(db)
    return sim_engine.advance(db, body.days)


@router.post("/reset", response_model=SimStateOut)
def reset(body: ResetRequest | None = None, db: Session = Depends(get_db)):
    """Wipe all data and regenerate it. Uses the given seed, else the current one, else 42."""
    _block_while_planning(db)
    seed = body.seed if body and body.seed is not None else None
    if seed is None:
        try:
            seed = sim_engine.get_seed(db)
        except RuntimeError:
            seed = 42
    engine = db.get_bind()
    db.close()  # release the connection before dropping tables
    reseed(engine, seed)
    with Session(engine) as fresh:
        return _build_state(fresh)


@router.get("/progress", response_model=list[CourseProgress])
def progress(db: Session = Depends(get_db)):
    """Rough per-course progress: target, attended so far, booked upcoming, planned seats still to come."""
    now = get_sim_now(db)
    attended = dict(db.execute(
        select(TrainingSession.course_id, func.count(Enrollment.id))
        .join(Enrollment, Enrollment.session_id == TrainingSession.id)
        .where(Enrollment.status == "attended")
        .group_by(TrainingSession.course_id)
    ).all())
    booked = dict(db.execute(
        select(TrainingSession.course_id, func.count(Enrollment.id))
        .join(Enrollment, Enrollment.session_id == TrainingSession.id)
        .where(Enrollment.status == "booked", TrainingSession.status == "scheduled")
        .group_by(TrainingSession.course_id)
    ).all())
    capacity_left = dict(db.execute(
        select(TrainingSession.course_id, func.sum(TrainingSession.capacity))
        .where(TrainingSession.status == "scheduled", TrainingSession.start_time >= now)
        .group_by(TrainingSession.course_id)
    ).all())
    rows = db.execute(
        select(Course, TrainingTarget.target_completions)
        .join(TrainingTarget, TrainingTarget.course_id == Course.id)
        .where(TrainingTarget.year == SIM_YEAR)
        .order_by(Course.id)
    ).all()
    return [
        CourseProgress(
            course_id=c.id, code=c.code, name=c.name, is_mandatory=c.is_mandatory, target=target,
            attended=attended.get(c.id, 0),
            percent_of_target=round(100 * attended.get(c.id, 0) / target, 1) if target else 0.0,
            booked_upcoming=booked.get(c.id, 0),
            planned_capacity_remaining=int(capacity_left.get(c.id, 0) or 0),
        )
        for c, target in rows
    ]
