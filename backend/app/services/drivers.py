"""Drivers and their unavailability blocks."""
import re
from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.live.bus import publish_after_commit

from app.models import Course, Driver, DriverUnavailability, Enrollment, TrainingSession
from app.schemas.drivers import DriverCreate, DriverUpdate, UnavailabilityCreate
from app.services.audit import apply_changes, log_event
from app.services.errors import Conflict, Invalid, NotFound, get_or_404
from app.services.pagination import PageParams, paginate
from app.services.sim_clock import get_sim_now

SORTABLE = {
    "employee_code": Driver.employee_code,
    "name": Driver.name,
    "nationality": Driver.nationality,
    "shift": Driver.shift,
    "depot": Driver.depot,
    "hire_date": Driver.hire_date,
    "is_active": Driver.is_active,
}


def list_drivers(db: Session, params: PageParams, *, q=None, nationality=None, shift=None, depot=None, is_active=None):
    stmt = select(Driver)
    if q:
        like = f"%{q.strip().lower()}%"
        stmt = stmt.where(or_(Driver.name.ilike(like), Driver.employee_code.ilike(like)))
    if nationality:
        stmt = stmt.where(Driver.nationality == nationality)
    if shift:
        stmt = stmt.where(Driver.shift == shift)
    if depot:
        stmt = stmt.where(Driver.depot == depot)
    if is_active is not None:
        stmt = stmt.where(Driver.is_active.is_(is_active))
    rows, total = paginate(db, stmt, params, SORTABLE, "employee_code", Driver.id)
    return [r[0] for r in rows], total


def options(db: Session) -> dict:
    def distinct(column):
        return list(db.scalars(select(column).distinct().order_by(column)))

    return {
        "nationalities": distinct(Driver.nationality),
        "shifts": distinct(Driver.shift),
        "depots": distinct(Driver.depot),
    }


def _next_employee_code(db: Session) -> str:
    """Next free DRV-xxxx (one higher than the largest existing number)."""
    highest = 0
    for code in db.scalars(select(Driver.employee_code)):
        match = re.fullmatch(r"DRV-(\d+)", code)
        if match:
            highest = max(highest, int(match.group(1)))
    return f"DRV-{highest + 1:04d}"


def _check_code_free(db: Session, code: str, exclude_id: int | None = None) -> None:
    clash = db.scalars(select(Driver).where(Driver.employee_code == code)).first()
    if clash is not None and clash.id != exclude_id:
        raise Conflict(f"Employee code {code} is already used by {clash.name}", {"employee_code": "already in use"})


def create_driver(db: Session, data: DriverCreate) -> Driver:
    now = get_sim_now(db)
    code = data.employee_code or _next_employee_code(db)
    _check_code_free(db, code)
    driver = Driver(**data.model_dump(exclude={"employee_code"}), employee_code=code)
    db.add(driver)
    db.flush()
    log_event(db, now, "user", "driver_created", "driver", driver.id, {
        "employee_code": code, "name": driver.name, "shift": driver.shift,
        "nationality": driver.nationality, "depot": driver.depot,
    })
    publish_after_commit(db, "master_data.changed", {"entity": "drivers"})
    db.commit()
    return driver


def update_driver(db: Session, driver_id: int, data: DriverUpdate) -> Driver:
    now = get_sim_now(db)
    driver = get_or_404(db, Driver, driver_id, "Driver")
    changes = {k: v for k, v in data.model_dump(exclude_unset=True).items() if v is not None}
    if changes.get("is_active") is False and driver.is_active:
        raise Conflict("Use DELETE /api/drivers/{id} to deactivate a driver, so their upcoming bookings are cancelled too")
    if "employee_code" in changes:
        _check_code_free(db, changes["employee_code"], exclude_id=driver.id)
    diff = apply_changes(driver, changes)
    if diff:
        log_event(db, now, "user", "driver_updated", "driver", driver.id, diff)
        publish_after_commit(db, "sessions.changed", {"session_ids": []})  # bookings may have been cancelled
        publish_after_commit(db, "master_data.changed", {"entity": "drivers"})
        db.commit()
    return driver


def _cancel_bookings(db: Session, driver_id: int, now: datetime, window_start: datetime | None = None, window_end: datetime | None = None) -> int:
    """Cancel the driver's booked enrollments in scheduled sessions that have not started yet.

    With a window, only sessions overlapping [window_start, window_end) are touched.
    """
    stmt = (
        select(Enrollment)
        .join(TrainingSession, TrainingSession.id == Enrollment.session_id)
        .where(
            Enrollment.driver_id == driver_id,
            Enrollment.status == "booked",
            TrainingSession.status == "scheduled",
            TrainingSession.start_time > now,
        )
    )
    if window_start is not None and window_end is not None:
        stmt = stmt.where(TrainingSession.start_time < window_end, TrainingSession.end_time > window_start)
    count = 0
    for enrollment in db.scalars(stmt):
        enrollment.status = "cancelled"
        count += 1
    return count


def deactivate_driver(db: Session, driver_id: int) -> tuple[Driver, int]:
    """Drivers keep their history, so 'delete' just switches them off and frees their future seats."""
    now = get_sim_now(db)
    driver = get_or_404(db, Driver, driver_id, "Driver")
    cancelled = _cancel_bookings(db, driver.id, now)
    driver.is_active = False
    log_event(db, now, "user", "driver_deactivated", "driver", driver.id, {"cancelled_bookings": cancelled})
    publish_after_commit(db, "sessions.changed", {"session_ids": []})  # bookings may have been cancelled
    publish_after_commit(db, "master_data.changed", {"entity": "drivers"})
    db.commit()
    return driver, cancelled


def _booking_rows(db: Session, driver_id: int) -> list[dict]:
    rows = db.execute(
        select(Enrollment, TrainingSession, Course)
        .join(TrainingSession, TrainingSession.id == Enrollment.session_id)
        .join(Course, Course.id == TrainingSession.course_id)
        .where(Enrollment.driver_id == driver_id)
        .order_by(TrainingSession.start_time.desc())
    ).all()
    return [
        {
            "enrollment_id": e.id, "session_id": s.id, "course_code": c.code, "course_name": c.name,
            "session_start": s.start_time, "session_status": s.status, "status": e.status,
        }
        for e, s, c in rows
    ]


def driver_detail(db: Session, driver_id: int) -> dict:
    driver = get_or_404(db, Driver, driver_id, "Driver")
    now = get_sim_now(db)
    bookings = _booking_rows(db, driver_id)
    upcoming = [
        b for b in bookings
        if b["status"] == "booked" and b["session_status"] == "scheduled" and b["session_start"] > now
    ]
    upcoming.sort(key=lambda b: b["session_start"])
    history = [b for b in bookings if b["session_start"] <= now or b["status"] != "booked"]
    return {
        "id": driver.id, "employee_code": driver.employee_code, "name": driver.name,
        "nationality": driver.nationality, "shift": driver.shift, "depot": driver.depot,
        "hire_date": driver.hire_date, "is_active": driver.is_active,
        "history": history, "upcoming": upcoming,
    }


# --- Unavailability -----------------------------------------------------------------------
def list_unavailability(db: Session, driver_id: int) -> list[DriverUnavailability]:
    get_or_404(db, Driver, driver_id, "Driver")
    return list(db.scalars(
        select(DriverUnavailability).where(DriverUnavailability.driver_id == driver_id).order_by(DriverUnavailability.start_time.desc())
    ))


def add_unavailability(db: Session, driver_id: int, data: UnavailabilityCreate) -> tuple[DriverUnavailability, int]:
    now = get_sim_now(db)
    get_or_404(db, Driver, driver_id, "Driver")
    if data.end_time <= data.start_time:
        raise Invalid("End must be after start", {"end_time": "must be after start"})
    block = DriverUnavailability(driver_id=driver_id, start_time=data.start_time, end_time=data.end_time, reason=data.reason)
    db.add(block)
    db.flush()
    cancelled = _cancel_bookings(db, driver_id, now, data.start_time, data.end_time)
    log_event(db, now, "user", "driver_unavailability_added", "driver_unavailability", block.id, {
        "driver_id": driver_id, "start_time": data.start_time.isoformat(), "end_time": data.end_time.isoformat(),
        "reason": data.reason, "cancelled_bookings": cancelled,
    })
    publish_after_commit(db, "sessions.changed", {"session_ids": []})  # bookings may have been cancelled
    publish_after_commit(db, "master_data.changed", {"entity": "drivers"})
    db.commit()
    return block, cancelled


def remove_unavailability(db: Session, driver_id: int, uid: int) -> None:
    now = get_sim_now(db)
    block = get_or_404(db, DriverUnavailability, uid, "Unavailability block")
    if block.driver_id != driver_id:
        raise NotFound(f"Unavailability block {uid} not found for driver {driver_id}")
    log_event(db, now, "user", "driver_unavailability_removed", "driver_unavailability", block.id, {
        "driver_id": driver_id, "start_time": block.start_time.isoformat(),
        "end_time": block.end_time.isoformat(), "reason": block.reason,
    })
    db.delete(block)
    publish_after_commit(db, "master_data.changed", {"entity": "drivers"})
    db.commit()
