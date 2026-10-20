"""Who is booked into a session, and who is allowed to be.

``driver_block_reason`` is the single rule set. The POST endpoint and the eligible-drivers list
both call it, so the list can never offer someone the POST would reject.
"""
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.live.bus import publish_after_commit

from app.models import Driver, DriverUnavailability, Enrollment, TrainingSession
from app.services.audit import log_event
from app.services.errors import Conflict, get_or_404
from app.services.sim_clock import get_sim_now
from app.simulator.rules import SIM_YEAR, is_driver_free

ELIGIBLE_LIMIT = 50
YEAR_START = datetime(SIM_YEAR, 1, 1)
YEAR_END = datetime(SIM_YEAR + 1, 1, 1)


@dataclass
class Context:
    """Everything the per-driver rules need, loaded once for a session."""

    session: TrainingSession
    booked_count: int
    already_enrolled: set[int] = field(default_factory=set)  # booked / attended / no_show in THIS session
    attended_course: set[int] = field(default_factory=set)  # attended this course in SIM_YEAR
    overlapping: dict[int, int] = field(default_factory=dict)  # driver_id -> other session id booked at the same time
    unavailability: dict[int, list[DriverUnavailability]] = field(default_factory=dict)


def session_block_reason(session: TrainingSession, booked_count: int, now: datetime) -> str | None:
    """Reasons nobody can be added to this session at all."""
    if session.status != "scheduled":
        return f"Session is {session.status}, so it cannot take new bookings"
    if session.start_time <= now:
        return "Session has already started"
    if booked_count >= session.capacity:
        return f"Session is full ({booked_count}/{session.capacity})"
    return None


def driver_block_reason(driver: Driver, ctx: Context) -> str | None:
    """Why this driver cannot be booked into the session, or None if they can."""
    if not driver.is_active:
        return f"{driver.name} is inactive"
    if driver.id in ctx.already_enrolled:
        return f"{driver.name} is already enrolled in this session"
    if driver.id in ctx.attended_course:
        return f"{driver.name} has already attended this course in {SIM_YEAR}"
    if driver.id in ctx.overlapping:
        return f"{driver.name} is booked in session {ctx.overlapping[driver.id]} at the same time"
    ok, reason = is_driver_free(driver, ctx.session.start_time, ctx.session.end_time, ctx.unavailability.get(driver.id, []))
    if not ok:
        return f"{driver.name}: {reason}"
    return None


def _load_context(db: Session, session: TrainingSession, driver_ids: list[int] | None) -> Context:
    """Load all lookups with one query each. ``driver_ids=None`` means every driver."""
    def only_these(column):
        return [] if driver_ids is None else [column.in_(driver_ids)]

    booked_count = db.scalar(
        select(func.count()).select_from(Enrollment).where(Enrollment.session_id == session.id, Enrollment.status == "booked")
    ) or 0
    ctx = Context(session=session, booked_count=booked_count)

    ctx.already_enrolled = set(db.scalars(
        select(Enrollment.driver_id).where(
            Enrollment.session_id == session.id, Enrollment.status != "cancelled", *only_these(Enrollment.driver_id)
        )
    ))
    ctx.attended_course = set(db.scalars(
        select(Enrollment.driver_id)
        .join(TrainingSession, TrainingSession.id == Enrollment.session_id)
        .where(
            TrainingSession.course_id == session.course_id,
            TrainingSession.start_time >= YEAR_START, TrainingSession.start_time < YEAR_END,
            Enrollment.status == "attended", *only_these(Enrollment.driver_id),
        )
    ))
    for driver_id, other_id in db.execute(
        select(Enrollment.driver_id, Enrollment.session_id)
        .join(TrainingSession, TrainingSession.id == Enrollment.session_id)
        .where(
            Enrollment.status == "booked", TrainingSession.status == "scheduled",
            TrainingSession.id != session.id,
            TrainingSession.start_time < session.end_time, TrainingSession.end_time > session.start_time,
            *only_these(Enrollment.driver_id),
        )
    ):
        ctx.overlapping[driver_id] = other_id
    for row in db.scalars(
        select(DriverUnavailability).where(
            DriverUnavailability.start_time < session.end_time, DriverUnavailability.end_time > session.start_time,
            *only_these(DriverUnavailability.driver_id),
        )
    ):
        ctx.unavailability.setdefault(row.driver_id, []).append(row)
    return ctx


def list_enrollments(db: Session, session_id: int) -> list[dict]:
    get_or_404(db, TrainingSession, session_id, "Session")
    rows = db.execute(
        select(Enrollment, Driver)
        .join(Driver, Driver.id == Enrollment.driver_id)
        .where(Enrollment.session_id == session_id)
        .order_by(Driver.name)
    ).all()
    return [_out(e, d) for e, d in rows]


def _out(e: Enrollment, d: Driver) -> dict:
    return {
        "id": e.id, "session_id": e.session_id, "driver_id": d.id, "employee_code": d.employee_code,
        "name": d.name, "shift": d.shift, "nationality": d.nationality, "status": e.status,
    }


def add_enrollment(db: Session, session_id: int, driver_id: int) -> dict:
    now = get_sim_now(db)
    session = get_or_404(db, TrainingSession, session_id, "Session")
    driver = get_or_404(db, Driver, driver_id, "Driver")
    ctx = _load_context(db, session, [driver.id])
    reason = session_block_reason(session, ctx.booked_count, now) or driver_block_reason(driver, ctx)
    if reason:
        raise Conflict(reason)

    # (session, driver) is unique, so a driver who was removed earlier gets their old row back.
    enrollment = db.scalars(
        select(Enrollment).where(Enrollment.session_id == session.id, Enrollment.driver_id == driver.id)
    ).first()
    if enrollment is None:
        enrollment = Enrollment(session_id=session.id, driver_id=driver.id, status="booked")
        db.add(enrollment)
    else:
        enrollment.status = "booked"
    db.flush()
    log_event(db, now, "user", "enrollment_created", "enrollment", enrollment.id, {
        "session_id": session.id, "driver_id": driver.id, "employee_code": driver.employee_code,
    })
    publish_after_commit(db, "sessions.changed", {"session_ids": [session.id]})
    db.commit()
    return _out(enrollment, driver)


def cancel_enrollment(db: Session, session_id: int, enrollment_id: int) -> dict:
    now = get_sim_now(db)
    enrollment = get_or_404(db, Enrollment, enrollment_id, "Enrollment")
    if enrollment.session_id != session_id:
        raise Conflict(f"Enrollment {enrollment_id} does not belong to session {session_id}")
    if enrollment.status != "booked":
        raise Conflict(f"Only booked enrollments can be removed; this one is {enrollment.status}")
    driver = db.get(Driver, enrollment.driver_id)
    enrollment.status = "cancelled"
    log_event(db, now, "user", "enrollment_cancelled", "enrollment", enrollment.id, {
        "session_id": session_id, "driver_id": driver.id, "employee_code": driver.employee_code,
    })
    publish_after_commit(db, "sessions.changed", {"session_ids": [session_id]})
    db.commit()
    return _out(enrollment, driver)


def eligible_drivers(db: Session, session_id: int, q: str | None = None) -> list[dict]:
    """Active drivers that ``add_enrollment`` would accept. Loads data once, then loops in Python."""
    now = get_sim_now(db)
    session = get_or_404(db, TrainingSession, session_id, "Session")
    ctx = _load_context(db, session, None)
    if session_block_reason(session, ctx.booked_count, now):
        return []
    stmt = select(Driver).where(Driver.is_active.is_(True)).order_by(Driver.name, Driver.id)
    if q:
        like = f"%{q.strip().lower()}%"
        stmt = stmt.where(Driver.name.ilike(like) | Driver.employee_code.ilike(like))
    result = []
    for driver in db.scalars(stmt):
        if driver_block_reason(driver, ctx) is None:
            result.append({
                "id": driver.id, "employee_code": driver.employee_code, "name": driver.name,
                "shift": driver.shift, "nationality": driver.nationality, "depot": driver.depot,
            })
            if len(result) >= ELIGIBLE_LIMIT:
                break
    return result

