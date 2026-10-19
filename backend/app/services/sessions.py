"""Training sessions: listing, create/update/cancel, the calendar feed and the light validation rules.

The ``check_*`` functions are small on purpose. Phase 4 folds them into the full constraint checker.
"""
from datetime import datetime, timedelta

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.live.bus import publish_after_commit

from app.models import Course, Enrollment, Trainer, TrainingSession
from app.schemas.sessions import SessionCreate, SessionUpdate
from app.services.audit import apply_changes, log_event
from app.services.errors import Conflict, Invalid, NotFound, get_or_404
from app.services.pagination import PageParams, paginate
from app.services.sim_clock import get_sim_now
from app.simulator.rules import SESSION_START_HOURS, SESSION_WEEKDAYS, session_end, week_start

# Fill bands: below LOW is "low", LOW up to (not including) HIGH is "medium", HIGH and above is "high".
FILL_LOW = 0.5
FILL_HIGH = 0.8
MIN_CAPACITY = 1
MAX_CAPACITY = 30
WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


# --- Fill rate ---------------------------------------------------------------------------
def fill_rate(status: str, capacity: int, booked: int, attended: int) -> float | None:
    """How full a session is, as a fraction (0.0 to 1.0, can exceed 1 only if data is odd).

    - scheduled: booked / capacity (seats promised so far)
    - completed: attended / capacity (seats actually used)
    - cancelled: None (a cancelled session has no fill rate)

    Reused by the Phase 3 forecasting, so keep the definition here only.
    """
    if status == "cancelled" or capacity <= 0:
        return None
    numerator = attended if status == "completed" else booked
    return numerator / capacity


def fill_band(status: str, rate: float | None) -> str:
    """'low' / 'medium' / 'high' / 'cancelled' for colouring the calendar."""
    if status == "cancelled" or rate is None:
        return "cancelled"
    if rate < FILL_LOW:
        return "low"
    if rate < FILL_HIGH:
        return "medium"
    return "high"


# --- Querying with enrollment counts in one query -----------------------------------------
def _counts_subquery():
    """booked / attended / no_show per session, so list and calendar need no per-row queries."""
    def count_of(status: str):
        return func.sum(case((Enrollment.status == status, 1), else_=0))

    return (
        select(
            Enrollment.session_id.label("session_id"),
            count_of("booked").label("booked"),
            count_of("attended").label("attended"),
            count_of("no_show").label("no_show"),
        )
        .group_by(Enrollment.session_id)
        .subquery()
    )


def _session_select():
    """Session + course + trainer + counts. Returns (statement, sortable columns)."""
    counts = _counts_subquery()
    booked = func.coalesce(counts.c.booked, 0)
    attended = func.coalesce(counts.c.attended, 0)
    no_show = func.coalesce(counts.c.no_show, 0)
    stmt = (
        select(TrainingSession, Course.code, Course.name, Trainer.name, booked, attended, no_show)
        .join(Course, Course.id == TrainingSession.course_id)
        .join(Trainer, Trainer.id == TrainingSession.trainer_id)
        .outerjoin(counts, counts.c.session_id == TrainingSession.id)
    )
    sortable = {
        "start_time": TrainingSession.start_time,
        "course": Course.code,
        "trainer": Trainer.name,
        "status": TrainingSession.status,
        "source": TrainingSession.source,
        "capacity": TrainingSession.capacity,
        "booked": booked,
        "location": TrainingSession.location,
    }
    return stmt, sortable


def _row_to_dict(row, now: datetime) -> dict:
    session, course_code, course_name, trainer_name, booked, attended, no_show = row
    rate = fill_rate(session.status, session.capacity, booked, attended)
    return {
        "id": session.id,
        "course_id": session.course_id,
        "course_code": course_code,
        "course_name": course_name,
        "trainer_id": session.trainer_id,
        "trainer_name": trainer_name,
        "start_time": session.start_time,
        "end_time": session.end_time,
        "location": session.location,
        "capacity": session.capacity,
        "status": session.status,
        "source": session.source,
        "booked": int(booked),
        "attended": int(attended),
        "no_show": int(no_show),
        "fill_rate": None if rate is None else round(rate, 3),
        "fill_band": fill_band(session.status, rate),
        "is_editable": session.status == "scheduled" and session.start_time > now,
    }


def list_sessions(
    db: Session, params: PageParams, *, course_id=None, trainer_id=None, status=None, source=None,
    start_from: datetime | None = None, start_to: datetime | None = None,
):
    now = get_sim_now(db)
    stmt, sortable = _session_select()
    if course_id is not None:
        stmt = stmt.where(TrainingSession.course_id == course_id)
    if trainer_id is not None:
        stmt = stmt.where(TrainingSession.trainer_id == trainer_id)
    if status:
        stmt = stmt.where(TrainingSession.status == status)
    if source:
        stmt = stmt.where(TrainingSession.source == source)
    if start_from is not None:
        stmt = stmt.where(TrainingSession.start_time >= start_from)
    if start_to is not None:
        stmt = stmt.where(TrainingSession.start_time <= start_to)
    rows, total = paginate(db, stmt, params, sortable, "start_time", TrainingSession.id)
    items = [_row_to_dict(r, now) for r in rows]
    _attach_calendar(db, items)
    return items, total


def get_session_out(db: Session, session_id: int) -> dict:
    now = get_sim_now(db)
    stmt, _ = _session_select()
    row = db.execute(stmt.where(TrainingSession.id == session_id)).first()
    if row is None:
        raise NotFound(f"Session {session_id} not found")
    item = _row_to_dict(row, now)
    _attach_calendar(db, [item])
    return item


def _queue_sync(db: Session, session_id: int, op: str = "upsert") -> None:
    from app.calendar.sync import enqueue_if_linked

    enqueue_if_linked(db, session_id, op)


def _attach_calendar(db: Session, items: list[dict]) -> None:
    """Add ``calendar: {status, html_link, last_synced_at}`` to each session dict (Google Calendar sync state)."""
    from app.calendar.ops import calendar_info  # local import: the calendar package imports this module

    info = calendar_info(db, [i["id"] for i in items])
    for item in items:
        item["calendar"] = info[item["id"]]


def calendar_events(
    db: Session, start: datetime, end: datetime, *, course_id=None, trainer_id=None, include_cancelled=False
) -> dict:
    """Lightweight events for the calendar. One query for the sessions (joined with counts)."""
    if end <= start:
        raise Invalid("end must be after start", {"end": "must be after start"})
    if end - start > timedelta(days=62):
        raise Invalid("The calendar range can be at most 62 days", {"end": "range too long"})
    now = get_sim_now(db)
    stmt, _ = _session_select()
    stmt = stmt.where(TrainingSession.start_time >= start, TrainingSession.start_time < end)
    if course_id is not None:
        stmt = stmt.where(TrainingSession.course_id == course_id)
    if trainer_id is not None:
        stmt = stmt.where(TrainingSession.trainer_id == trainer_id)
    if not include_cancelled:
        stmt = stmt.where(TrainingSession.status != "cancelled")
    events = []
    for row in db.execute(stmt.order_by(TrainingSession.start_time, TrainingSession.id)):
        s = _row_to_dict(row, now)
        events.append({
            "id": s["id"], "course_id": s["course_id"], "course_code": s["course_code"],
            "course_name": s["course_name"], "trainer_id": s["trainer_id"], "trainer_name": s["trainer_name"],
            "start": s["start_time"], "end": s["end_time"], "location": s["location"],
            "capacity": s["capacity"], "status": s["status"], "source": s["source"],
            "booked": s["booked"], "attended": s["attended"],
            "fill_rate": s["fill_rate"], "fill_band": s["fill_band"],
        })
    info = _calendar_info(db, [e["id"] for e in events])
    for e in events:
        e["calendar"] = info[e["id"]]
    return {"sim_now": now, "events": events}


def _calendar_info(db: Session, ids: list[int]) -> dict[int, dict]:
    from app.calendar.ops import calendar_info

    return calendar_info(db, ids)


# --- Light validation rules ----------------------------------------------------------------
def check_in_future(start: datetime, now: datetime) -> None:
    if start <= now:
        raise Invalid(f"Start time must be after the current simulated time ({now:%Y-%m-%d %H:%M})", {"start_time": "must be in the future"})


def check_slot(start: datetime) -> None:
    """Sessions start on the hour at one of the fixed times, on a working day (Sunday to Thursday)."""
    if start.weekday() not in SESSION_WEEKDAYS:
        raise Invalid(f"Sessions run Sunday to Thursday; {start:%Y-%m-%d} is a {WEEKDAY_NAMES[start.weekday()]}", {"start_time": "not a session day"})
    allowed = ", ".join(f"{h:02d}:00" for h in SESSION_START_HOURS)
    if start.hour not in SESSION_START_HOURS or start.minute != 0:
        raise Invalid(f"Sessions must start at {allowed}; got {start:%H:%M}", {"start_time": f"start at {allowed}"})


def check_capacity(capacity: int) -> None:
    if not MIN_CAPACITY <= capacity <= MAX_CAPACITY:
        raise Invalid(f"Capacity must be between {MIN_CAPACITY} and {MAX_CAPACITY}", {"capacity": "out of range"})


def check_trainer_free(db: Session, trainer_id: int, start: datetime, end: datetime, exclude_id: int | None = None) -> None:
    """Trainer must not have another (non-cancelled) session overlapping this one."""
    stmt = select(TrainingSession).where(
        TrainingSession.trainer_id == trainer_id,
        TrainingSession.status != "cancelled",
        TrainingSession.start_time < end,
        TrainingSession.end_time > start,
    )
    if exclude_id is not None:
        stmt = stmt.where(TrainingSession.id != exclude_id)
    clash = db.scalars(stmt).first()
    if clash is not None:
        raise Conflict(f"Trainer is already double booked: session {clash.id} runs {clash.start_time:%Y-%m-%d %H:%M} to {clash.end_time:%H:%M}", {"trainer_id": "double booked"})


def sessions_in_week(db: Session, trainer_id: int, any_day: datetime, exclude_id: int | None = None) -> int:
    """Non-cancelled sessions the trainer has in the Sunday-to-Saturday week containing ``any_day``."""
    ws = datetime.combine(week_start(any_day.date()), datetime.min.time())
    stmt = select(func.count()).select_from(TrainingSession).where(
        TrainingSession.trainer_id == trainer_id,
        TrainingSession.status != "cancelled",
        TrainingSession.start_time >= ws,
        TrainingSession.start_time < ws + timedelta(days=7),
    )
    if exclude_id is not None:
        stmt = stmt.where(TrainingSession.id != exclude_id)
    return db.scalar(stmt) or 0


def check_trainer_weekly_load(db: Session, trainer: Trainer, start: datetime, exclude_id: int | None = None) -> None:
    existing = sessions_in_week(db, trainer.id, start, exclude_id)
    if existing + 1 > trainer.max_sessions_per_week:
        ws = week_start(start.date())
        raise Conflict(
            f"{trainer.name} already has {existing} of {trainer.max_sessions_per_week} sessions in the week starting {ws:%Y-%m-%d}",
            {"trainer_id": "weekly limit reached"},
        )


# --- Write operations ---------------------------------------------------------------------
def create_session(db: Session, data: SessionCreate) -> dict:
    now = get_sim_now(db)
    course = db.get(Course, data.course_id)
    if course is None:
        raise Invalid(f"Course {data.course_id} does not exist", {"course_id": "unknown course"})
    trainer = db.get(Trainer, data.trainer_id)
    if trainer is None:
        raise Invalid(f"Trainer {data.trainer_id} does not exist", {"trainer_id": "unknown trainer"})

    start = data.start_time
    end = data.end_time or session_end(start, course.duration_hours)
    capacity = data.capacity if data.capacity is not None else course.default_capacity
    if end <= start:
        raise Invalid("End time must be after start time", {"end_time": "must be after start"})
    check_in_future(start, now)
    check_slot(start)
    check_capacity(capacity)
    check_trainer_free(db, trainer.id, start, end)
    check_trainer_weekly_load(db, trainer, start)

    session = TrainingSession(
        course_id=course.id, trainer_id=trainer.id, start_time=start, end_time=end,
        location=data.location, capacity=capacity, status="scheduled", source="manual",
    )
    db.add(session)
    db.flush()
    log_event(db, now, "user", "session_created", "session", session.id, {
        "course": course.code, "trainer": trainer.name, "start_time": start.isoformat(),
        "end_time": end.isoformat(), "location": data.location, "capacity": capacity,
    })
    publish_after_commit(db, "sessions.changed", {"session_ids": [session.id]})
    db.commit()
    return get_session_out(db, session.id)


def _booked_count(db: Session, session_id: int) -> int:
    return db.scalar(
        select(func.count()).select_from(Enrollment).where(Enrollment.session_id == session_id, Enrollment.status == "booked")
    ) or 0


def update_session(db: Session, session_id: int, data: SessionUpdate) -> dict:
    now = get_sim_now(db)
    session = get_or_404(db, TrainingSession, session_id, "Session")
    if session.status != "scheduled":
        raise Conflict(f"A {session.status} session is read-only")
    if session.start_time <= now:
        raise Conflict("This session has already started and can no longer be edited")

    changes = data.model_dump(exclude_unset=True)
    changes = {k: v for k, v in changes.items() if v is not None}  # null never clears a required field
    course = db.get(Course, session.course_id)

    new_start = changes.get("start_time", session.start_time)
    if "start_time" in changes and "end_time" not in changes:
        changes["end_time"] = session_end(new_start, course.duration_hours)  # keep the length in step
    new_end = changes.get("end_time", session.end_time)
    new_trainer_id = changes.get("trainer_id", session.trainer_id)
    trainer = db.get(Trainer, new_trainer_id)
    if trainer is None:
        raise Invalid(f"Trainer {new_trainer_id} does not exist", {"trainer_id": "unknown trainer"})

    if new_end <= new_start:
        raise Invalid("End time must be after start time", {"end_time": "must be after start"})
    if "capacity" in changes:
        check_capacity(changes["capacity"])
        booked = _booked_count(db, session.id)
        if changes["capacity"] < booked:
            raise Conflict(f"Capacity cannot be lower than the {booked} drivers already booked; remove some bookings first", {"capacity": "below booked count"})
    if "start_time" in changes or "end_time" in changes or "trainer_id" in changes:
        if "start_time" in changes:
            check_in_future(new_start, now)
            check_slot(new_start)
        check_trainer_free(db, trainer.id, new_start, new_end, exclude_id=session.id)
        check_trainer_weekly_load(db, trainer, new_start, exclude_id=session.id)

    diff = apply_changes(session, changes)
    if diff:
        log_event(db, now, "user", "session_updated", "session", session.id, diff)
        _queue_sync(db, session.id)
        publish_after_commit(db, "sessions.changed", {"session_ids": [session.id]})
        db.commit()
    return get_session_out(db, session.id)


def cancel_session(db: Session, session_id: int) -> dict:
    now = get_sim_now(db)
    session = get_or_404(db, TrainingSession, session_id, "Session")
    if session.status != "scheduled":
        raise Conflict(f"Only scheduled sessions can be cancelled; this one is {session.status}")
    cancelled = 0
    for enrollment in db.scalars(select(Enrollment).where(Enrollment.session_id == session.id, Enrollment.status == "booked")):
        enrollment.status = "cancelled"
        cancelled += 1
    session.status = "cancelled"
    log_event(db, now, "user", "session_cancelled", "session", session.id, {"cancelled_bookings": cancelled})
    _queue_sync(db, session.id, "cancel")
    publish_after_commit(db, "sessions.changed", {"session_ids": [session.id]})
    db.commit()
    return get_session_out(db, session.id)
