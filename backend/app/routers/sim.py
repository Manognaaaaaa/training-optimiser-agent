from collections.abc import Callable

from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.live.autoplay import MIN_INTERVAL_SECONDS, autoplay
from app.live.bus import bus
from app.database import get_db
from app.config import settings
from app.llm import get_llm_client
from app.llm.base import LLMError
from app.models import Alert, Course, Driver, Enrollment, PlannerRun, TrainingSession, TrainingTarget
from app.routers.planner import get_session_factory, queue_run
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
def advance_clock(
    body: AdvanceRequest, background: BackgroundTasks, db: Session = Depends(get_db),
    session_factory: Callable[[], Session] = Depends(get_session_factory),
):
    """Play the simulation forward by ``days`` days (stops at year end)."""
    _block_while_planning(db)
    summary = sim_engine.advance(db, body.days)
    if settings.auto_plan_on_high_risk:
        _auto_plan(db, background, session_factory)
    return summary


def _auto_plan(db: Session, background: BackgroundTasks, session_factory: Callable[[], Session]) -> None:
    """AUTO_PLAN_ON_HIGH_RISK: queue plan generation for one open high-risk alert that has never been planned.

    Only one run can be active at a time, so the next alert is picked up after a later advance.
    """
    try:
        llm = get_llm_client()
    except LLMError:
        return  # no key configured: skip quietly
    planned = select(PlannerRun.alert_id)
    alert = db.scalars(
        select(Alert).where(Alert.status == "open", Alert.risk_level == "high", Alert.id.not_in(planned)).order_by(Alert.id)
    ).first()
    if alert is not None:
        queue_run(db, alert, None, llm, background, session_factory)


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
    bus.publish("resync", {"reason": "database re-seeded"})
    with Session(engine) as fresh:
        return _build_state(fresh)


class AutoplayRequest(BaseModel):
    enabled: bool
    step: Literal["day", "week"] = "day"
    interval_seconds: float = Field(5, ge=MIN_INTERVAL_SECONDS, le=300)


@router.get("/autoplay")
def get_autoplay():
    return autoplay.state()


@router.post("/autoplay")
async def set_autoplay(body: AutoplayRequest):
    """Start or stop advancing the simulated clock on a timer."""
    if body.enabled:
        return await autoplay.start(body.step, body.interval_seconds)
    return await autoplay.stop()


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
