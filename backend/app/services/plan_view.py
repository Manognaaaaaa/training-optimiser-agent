"""Turns a stored plan's actions into a readable change list (real names, ids and dates from the database).

Used by the alert writer (the fact pack the LLM sees) and by the alerts API (the change tables in the inbox),
so both always show exactly the same facts.
"""
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Driver, Trainer, TrainingSession


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def when(value: datetime | str | None) -> str | None:
    """Format a time like 'Thu 21 May 09:00' (the same format the planner page uses)."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    return f"{value:%a %d %b %H:%M}"


def describe_actions(db: Session, actions: Any) -> list[dict]:
    """One dict per action, in order, so a ``new:N`` reference resolves to the add_session before it."""
    actions = [a for a in actions if isinstance(a, dict)] if isinstance(actions, list) else []
    driver_ids: set[int] = set()
    session_ids: set[int] = set()
    trainer_ids: set[int] = set()
    for a in actions:
        for key, bucket in (("driver_id", driver_ids), ("session_id", session_ids), ("from_session_id", session_ids),
                            ("to_session_id", session_ids), ("trainer_id", trainer_ids)):
            if _is_int(a.get(key)):
                bucket.add(a[key])
    drivers = {d.id: d for d in db.scalars(select(Driver).where(Driver.id.in_(driver_ids)))} if driver_ids else {}
    sessions = {s.id: s for s in db.scalars(select(TrainingSession).where(TrainingSession.id.in_(session_ids)))} if session_ids else {}
    trainers = {t.id: t for t in db.scalars(select(Trainer).where(Trainer.id.in_(trainer_ids)))} if trainer_ids else {}
    new_sessions: dict[str, str | None] = {}

    def session_start(ref) -> str | None:
        if isinstance(ref, str):
            return new_sessions.get(ref)
        row = sessions.get(ref)
        return when(row.start_time) if row else None

    def driver_fields(driver_id) -> dict:
        d = drivers.get(driver_id)
        return {"driver_id": driver_id, "driver_name": d.name if d else None,
                "shift": d.shift if d else None, "nationality": d.nationality if d else None}

    out: list[dict] = []
    for a in actions:
        kind = a.get("type")
        if kind == "add_session":
            ref = str(a.get("temp_ref"))
            new_sessions[ref] = when(a.get("start"))
            trainer = trainers.get(a.get("trainer_id"))
            out.append({
                "type": "add_session", "temp_ref": ref, "course_id": a.get("course_id"), "trainer_id": a.get("trainer_id"),
                "trainer_name": trainer.name if trainer else None, "start": a.get("start"), "end": a.get("end"),
                "when": when(a.get("start")), "location": a.get("location"), "capacity": a.get("capacity"),
            })
        elif kind == "enroll_driver":
            ref = a.get("session_id")
            out.append({"type": "enroll_driver", **driver_fields(a.get("driver_id")), "session_id": ref, "when": session_start(ref)})
        elif kind == "move_driver":
            src, dst = a.get("from_session_id"), a.get("to_session_id")
            out.append({
                "type": "move_driver", **driver_fields(a.get("driver_id")),
                "from_session_id": src, "from_when": session_start(src), "to_session_id": dst, "to_when": session_start(dst),
            })
    return out
