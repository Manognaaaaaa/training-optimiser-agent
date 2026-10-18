"""Read-only tools the planning agent can call.

Each tool is an OpenAI-style JSON schema plus a Python function ``(db, now, args) -> dict``.
Rules for every tool:
- It only runs SELECT queries (the agent can never change data).
- Bad arguments come back as ``{"error": "..."}`` for the model to read, never as an exception.
- Results are compact and capped at ``MAX_TOOL_ROWS`` rows (``"truncated": true`` when cut).
- A driver's nationality is never returned (see docs/agent_planner.md for why).
- "Would this be allowed?" questions are answered by the checker's own rule lists, not by copies of them.

``validate_plan``, ``submit_plan`` and ``submit_no_plan`` are listed here so the model sees their
schemas, but the planner runs them itself because they need per-sample state.
"""
import math
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Alert, Course, Driver, Enrollment, Trainer, TrainingSession, TrainingTarget
from app.schemas.plan import AddSession
from app.services.constraints import (
    SessionInfo, driver_can_book, load_snapshot_for_ids, trainer_can_run,
)
from app.services.sessions import fill_rate
from app.simulator.rules import (
    BOOKING_WINDOW_DAYS, SESSION_START_HOURS, SESSION_WEEKDAYS, SIM_YEAR, session_end,
)

MAX_TOOL_ROWS = 25
DEFAULT_LIMIT = 15
SLOT_START_HOURS = SESSION_START_HOURS  # the grid used for new-session slots: 09:00, 14:00, 18:00
SLOT_WEEKDAYS = SESSION_WEEKDAYS  # Sunday to Thursday
MAX_SLOT_RANGE_DAYS = 28
DEFAULT_SLOT_RANGE_DAYS = 14
SLOT_LOCATION = "Training Room"


# --- Argument models ---------------------------------------------------------------------------
class _Args(BaseModel):
    model_config = ConfigDict(extra="ignore")


class AlertArgs(_Args):
    alert_id: int


class UpcomingArgs(_Args):
    course_id: int
    from_date: date | None = None
    to_date: date | None = None
    min_free_seats: int = Field(default=0, ge=0)


class RosterArgs(_Args):
    session_id: int


class EligibleArgs(_Args):
    course_id: int
    session_id: int
    limit: int = Field(default=DEFAULT_LIMIT, ge=1)
    move_from_session_id: int | None = None  # also consider drivers booked in this session (for a move_driver)


class SlotArgs(_Args):
    course_id: int
    from_date: date | None = None
    to_date: date | None = None
    limit: int = Field(default=DEFAULT_LIMIT, ge=1)


def _clip(limit: int) -> int:
    return min(limit, MAX_TOOL_ROWS)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="minutes")


def _booked_counts(db: Session, session_ids: list[int]) -> dict[int, int]:
    if not session_ids:
        return {}
    rows = db.execute(
        select(Enrollment.session_id, func.count(Enrollment.id))
        .where(Enrollment.session_id.in_(session_ids), Enrollment.status == "booked")
        .group_by(Enrollment.session_id)
    )
    return {session_id: count for session_id, count in rows}


# --- Tools -------------------------------------------------------------------------------------
def get_alert_context(db: Session, now: datetime, args: AlertArgs) -> dict:
    alert = db.get(Alert, args.alert_id)
    if alert is None:
        return {"error": f"Alert {args.alert_id} does not exist."}
    course = db.get(Course, alert.course_id)
    target = db.scalar(
        select(TrainingTarget.target_completions).where(TrainingTarget.course_id == alert.course_id, TrainingTarget.year == SIM_YEAR)
    )
    details = alert.details or {}
    projected = details.get("projected", alert.projected_completions)
    return {
        "alert_id": alert.id,
        "status": alert.status,
        "course": {
            "id": course.id, "code": course.code, "name": course.name,
            "duration_hours": course.duration_hours, "default_capacity": course.default_capacity,
        },
        "target": target if target is not None else alert.target_completions,
        "attended": details.get("attended"),
        "projected": projected,
        "low": details.get("low"),
        "high": details.get("high"),
        "p_hit": details.get("p_hit"),
        "shortfall_type": alert.shortfall_type,
        "reasons": [{"code": r.get("code"), "message": r.get("message")} for r in details.get("reasons", [])],
        "gap": max(0, math.ceil((target if target is not None else alert.target_completions) - projected)),
        "eligible_pool": details.get("eligible_pool"),
        "sim_now": _iso(now),
        "booking_window_days": BOOKING_WINDOW_DAYS,
        "session_slots": {"start_hours": list(SLOT_START_HOURS), "days": "Sunday to Thursday"},
    }


def list_upcoming_sessions(db: Session, now: datetime, args: UpcomingArgs) -> dict:
    if db.get(Course, args.course_id) is None:
        return {"error": f"Course {args.course_id} does not exist."}
    query = select(TrainingSession).where(
        TrainingSession.course_id == args.course_id, TrainingSession.status == "scheduled", TrainingSession.start_time > now,
    )
    if args.from_date:
        query = query.where(TrainingSession.start_time >= datetime.combine(args.from_date, time.min))
    if args.to_date:
        query = query.where(TrainingSession.start_time < datetime.combine(args.to_date + timedelta(days=1), time.min))
    rows = list(db.scalars(query.order_by(TrainingSession.start_time, TrainingSession.id)))
    booked = _booked_counts(db, [r.id for r in rows])
    window_end = now + timedelta(days=BOOKING_WINDOW_DAYS)
    out = []
    for row in rows:
        free = row.capacity - booked.get(row.id, 0)
        if free < args.min_free_seats:
            continue
        out.append({
            "session_id": row.id, "start": _iso(row.start_time), "end": _iso(row.end_time), "trainer_id": row.trainer_id,
            "capacity": row.capacity, "booked": booked.get(row.id, 0), "free_seats": free,
            "fill_rate": round(fill_rate("scheduled", row.capacity, booked.get(row.id, 0), 0) or 0, 2),
            "inside_booking_window": row.start_time <= window_end,
        })
    return {"sessions": out[:MAX_TOOL_ROWS], "total_matching": len(out), "truncated": len(out) > MAX_TOOL_ROWS}


def get_session_roster(db: Session, now: datetime, args: RosterArgs) -> dict:
    session = db.get(TrainingSession, args.session_id)
    if session is None:
        return {"error": f"Session {args.session_id} does not exist."}
    drivers = list(db.scalars(
        select(Driver).join(Enrollment, Enrollment.driver_id == Driver.id)
        .where(Enrollment.session_id == session.id, Enrollment.status == "booked").order_by(Driver.id)
    ))
    return {
        "session_id": session.id, "start": _iso(session.start_time), "capacity": session.capacity,
        "drivers": [{"driver_id": d.id, "name": d.name, "shift": d.shift, "depot": d.depot} for d in drivers[:MAX_TOOL_ROWS]],
        "total_booked": len(drivers), "truncated": len(drivers) > MAX_TOOL_ROWS,
    }


def _driver_row(driver: Driver) -> dict:
    return {"driver_id": driver.id, "name": driver.name, "shift": driver.shift, "depot": driver.depot}


def find_eligible_drivers(db: Session, now: datetime, args: EligibleArgs) -> dict:
    course = db.get(Course, args.course_id)
    session = db.get(TrainingSession, args.session_id)
    if course is None:
        return {"error": f"Course {args.course_id} does not exist."}
    if session is None:
        return {"error": f"Session {args.session_id} does not exist."}
    if session.course_id != course.id:
        return {"error": f"Session {session.id} belongs to course {session.course_id}, not course {course.id}."}
    if session.status != "scheduled" or session.start_time <= now:
        return {"error": f"Session {session.id} is not an upcoming scheduled session, so nobody can be booked into it."}

    year = (datetime(SIM_YEAR, 1, 1), datetime(SIM_YEAR + 1, 1, 1))
    attended = select(Enrollment.driver_id).join(TrainingSession, TrainingSession.id == Enrollment.session_id).where(
        Enrollment.status == "attended", TrainingSession.course_id == course.id,
        TrainingSession.start_time >= year[0], TrainingSession.start_time < year[1],
    )
    booked = select(Enrollment.driver_id).join(TrainingSession, TrainingSession.id == Enrollment.session_id).where(
        Enrollment.status == "booked", TrainingSession.course_id == course.id, TrainingSession.status == "scheduled",
    )
    candidates = db.scalars(
        select(Driver).where(Driver.is_active.is_(True), Driver.id.not_in(attended), Driver.id.not_in(booked)).order_by(Driver.id)
    ).all()

    # For a move, the drivers already in the "from" session are candidates too (they are booked, so the query above skips them).
    movers: list[Driver] = []
    if args.move_from_session_id is not None:
        source = db.get(TrainingSession, args.move_from_session_id)
        if source is None:
            return {"error": f"Session {args.move_from_session_id} does not exist."}
        if source.course_id != course.id:
            return {"error": f"Session {source.id} belongs to a different course."}
        movers = list(db.scalars(
            select(Driver).join(Enrollment, Enrollment.driver_id == Driver.id)
            .where(Enrollment.session_id == source.id, Enrollment.status == "booked", Driver.is_active.is_(True),
                   Driver.id.not_in(attended)).order_by(Driver.id)
        ))
        candidates = movers  # only movers make sense when a source session is given
    driver_ids = {d.id for d in candidates}
    snap = load_snapshot_for_ids(db, course.id, now, driver_ids, {session.id}, set())
    info = snap.sessions[session.id]

    found: list[dict] = []
    total = 0
    for driver in candidates:
        if args.move_from_session_id is not None:
            snap.unbook(driver.id, args.move_from_session_id)  # the move frees the old seat first, like the checker does
        ok = not driver_can_book(snap, driver, info)
        if args.move_from_session_id is not None:
            snap.book(driver.id, args.move_from_session_id)
        if ok:
            total += 1
            if len(found) < _clip(args.limit):
                found.append(_driver_row(driver))
    return {
        "session_id": session.id, "free_seats": session.capacity - len(snap.roster[session.id]),
        "drivers": found, "total_eligible": total, "truncated": total > len(found),
    }


def find_trainer_slots(db: Session, now: datetime, args: SlotArgs) -> dict:
    course = db.get(Course, args.course_id)
    if course is None:
        return {"error": f"Course {args.course_id} does not exist."}
    start_day = args.from_date or now.date()
    end_day = args.to_date or (start_day + timedelta(days=DEFAULT_SLOT_RANGE_DAYS))
    if end_day < start_day:
        return {"error": "to_date is before from_date."}
    if (end_day - start_day).days > MAX_SLOT_RANGE_DAYS:
        return {"error": f"Date range too long; the maximum is {MAX_SLOT_RANGE_DAYS} days."}

    trainer_ids = set(db.scalars(select(Trainer.id)))
    snap = load_snapshot_for_ids(db, course.id, now, set(), set(), trainer_ids)
    out: list[dict] = []
    total = 0
    day = start_day
    while day <= end_day:
        if day.weekday() in SLOT_WEEKDAYS:
            for hour in SLOT_START_HOURS:
                start = datetime.combine(day, time(hour))
                end = session_end(start, course.duration_hours)
                for trainer_id in sorted(trainer_ids):
                    candidate = AddSession(
                        type="add_session", temp_ref="new:1", course_id=course.id, trainer_id=trainer_id,
                        start=start, end=end, location=SLOT_LOCATION, capacity=course.default_capacity,
                    )
                    if not trainer_can_run(snap, candidate):
                        total += 1
                        if len(out) < _clip(args.limit):
                            out.append({"trainer_id": trainer_id, "start": _iso(start), "end": _iso(end), "max_capacity": course.default_capacity})
        day += timedelta(days=1)
    return {"slots": out, "total_matching": total, "truncated": total > len(out)}


# --- Registry -----------------------------------------------------------------------------------
READ_ONLY_TOOLS: dict[str, tuple[type[BaseModel], Callable[[Session, datetime, Any], dict]]] = {
    "get_alert_context": (AlertArgs, get_alert_context),
    "list_upcoming_sessions": (UpcomingArgs, list_upcoming_sessions),
    "get_session_roster": (RosterArgs, get_session_roster),
    "find_eligible_drivers": (EligibleArgs, find_eligible_drivers),
    "find_trainer_slots": (SlotArgs, find_trainer_slots),
}


def run_read_only_tool(db: Session, now: datetime, name: str, args: Any) -> dict:
    """Run one read-only tool. Always returns a dict; problems become ``{"error": ...}``."""
    entry = READ_ONLY_TOOLS.get(name)
    if entry is None:
        return {"error": f"Unknown tool '{name}'."}
    if not isinstance(args, dict):
        return {"error": "Tool arguments must be a JSON object."}
    model, func_ = entry
    try:
        parsed = model.model_validate(args)
    except ValidationError as err:
        first = err.errors()[0]
        return {"error": f"Bad arguments: {'.'.join(str(p) for p in first['loc'])}: {first['msg']}"}
    return func_(db, now, parsed)


def _spec(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties, "required": required},
    }}


_PLAN_PARAM = {"plan": {"type": "object", "description": "The plan JSON in the exact format given in the system prompt.", "additionalProperties": True}}

TOOL_SPECS: list[dict] = [
    _spec("get_alert_context", "Facts about the at-risk course and the shortfall.", {"alert_id": {"type": "integer"}}, ["alert_id"]),
    _spec(
        "list_upcoming_sessions", "Scheduled future sessions of a course with free seats.",
        {"course_id": {"type": "integer"}, "from_date": {"type": "string", "description": "YYYY-MM-DD"},
         "to_date": {"type": "string", "description": "YYYY-MM-DD"}, "min_free_seats": {"type": "integer"}},
        ["course_id"],
    ),
    _spec("get_session_roster", "Drivers booked into a session (id, name, shift, depot).", {"session_id": {"type": "integer"}}, ["session_id"]),
    _spec(
        "find_eligible_drivers",
        "Drivers who can be booked into a session without breaking any rule. Pass move_from_session_id to list drivers "
        "already in that session who could be moved into the target session.",
        {"course_id": {"type": "integer"}, "session_id": {"type": "integer"}, "limit": {"type": "integer"},
         "move_from_session_id": {"type": "integer"}},
        ["course_id", "session_id"],
    ),
    _spec(
        "find_trainer_slots", "Slots (trainer, start, end) where a NEW session of the course would pass the rules.",
        {"course_id": {"type": "integer"}, "from_date": {"type": "string"}, "to_date": {"type": "string"}, "limit": {"type": "integer"}},
        ["course_id"],
    ),
    _spec("validate_plan", "Check a draft plan with the real rule checker. At most 2 calls per attempt.", _PLAN_PARAM, ["plan"]),
    _spec("submit_plan", "Submit the final plan. Ends this attempt.", _PLAN_PARAM, ["plan"]),
    _spec(
        "submit_no_plan", "Use only if no valid fix exists. Ends this attempt.",
        {"reason": {"type": "string"}}, ["reason"],
    ),
]
