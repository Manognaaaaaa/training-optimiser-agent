"""Turn a training session into a Google Calendar event body. Pure: same database state, same output.

Google colour ids (same bands as the in-app calendar, see ``services.sessions.fill_band``):
  high fill (>= 80%)   -> 10 (basil, green)
  medium fill (50-79%) -> 5  (banana, yellow)
  low fill (< 50%)     -> 11 (tomato, red)
Cancelled sessions are deleted from Google rather than coloured.

No attendees: a service account without domain-wide delegation cannot invite people, so the roster is in the
description instead.
"""
import base64
import hashlib
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.calendar.base import APP_TAG
from app.config import settings
from app.models import CalendarOutbox, Course, Driver, Enrollment, Plan, Trainer, TrainingSession
from app.services.sessions import fill_band, fill_rate

COLOR_BY_BAND = {"high": "10", "medium": "5", "low": "11", "cancelled": "8"}
ROSTER_STATUSES = ("booked", "attended")


def event_id_for(session_id: int, env: str | None = None) -> str:
    """Deterministic Google event id: "tos" + base32hex(sha1(env:session_id)), lowercase, at most 40 chars.

    Google allows only a-v and 0-9 in ids (length 5 to 1024), which is exactly the base32hex alphabet in lowercase.
    The same session always gets the same id, so a retry after a crash can never create a duplicate.
    """
    env = settings.app_env if env is None else env
    digest = hashlib.sha1(f"{env}:{session_id}".encode()).digest()
    return ("tos" + base64.b32hexencode(digest).decode().rstrip("=").lower())[:40]


def content_hash(body: dict) -> str:
    """Hash of the event body without its own ``content_hash`` property."""
    clean = json.loads(json.dumps(body))
    clean.get("extendedProperties", {}).get("private", {}).pop("content_hash", None)
    return hashlib.sha256(json.dumps(clean, sort_keys=True).encode()).hexdigest()


def plan_context(db: Session, session_id: int) -> dict | None:
    """The newest plan approval that touched this session: {plan_id, alert_id, rank, reason}, or None."""
    row = db.scalars(
        select(CalendarOutbox).where(
            CalendarOutbox.session_id == session_id, CalendarOutbox.source == "plan_approval", CalendarOutbox.plan_id.is_not(None))
        .order_by(CalendarOutbox.id.desc())
    ).first()
    if row is None:
        return None
    plan = db.get(Plan, row.plan_id)
    reason = ((plan.rationale or "").strip().replace("\n", " ")[:200]) if plan else ""
    return {"plan_id": row.plan_id, "alert_id": row.alert_id, "rank": plan.rank if plan else None, "reason": reason}


def build_event(session: TrainingSession, db: Session) -> dict:
    course = db.get(Course, session.course_id)
    trainer = db.get(Trainer, session.trainer_id)
    drivers = list(db.scalars(
        select(Driver).join(Enrollment, Enrollment.driver_id == Driver.id)
        .where(Enrollment.session_id == session.id, Enrollment.status.in_(ROSTER_STATUSES)).order_by(Driver.name)))
    enrolled = len(drivers)
    rate = fill_rate(session.status, session.capacity, enrolled, enrolled)
    band = fill_band(session.status, rate)
    plan = plan_context(db, session.id)
    from_plan = session.source == "agent"

    summary = f"{course.code}: {course.name}" + (" (added by plan)" if from_plan else "")
    lines = [
        f"Trainer: {trainer.name}",
        f"Capacity: {session.capacity}",
        f"Enrolled: {enrolled}",
        f"Fill rate: {round(100 * enrolled / session.capacity) if session.capacity else 0}%",
        "",
        "Roster:",
        *([d.name for d in drivers] or ["(nobody booked yet)"]),
    ]
    if plan:
        lines += ["", f"Plan {plan['plan_id']} for alert {plan['alert_id']}" + (f" (rank {plan['rank']})" if plan["rank"] else "")]
        if plan["reason"]:
            lines.append(f"Why: {plan['reason']}")
        if plan["alert_id"]:
            lines.append(f"{settings.app_base_url.rstrip('/')}/alerts/{plan['alert_id']}")

    tz = settings.calendar_timezone
    private = {"app": APP_TAG, "env": settings.app_env, "session_id": str(session.id)}
    if plan:
        private["plan_id"] = str(plan["plan_id"])
    body = {
        "summary": summary,
        "location": session.location,
        "description": "\n".join(lines),
        "start": {"dateTime": session.start_time.isoformat(timespec="seconds"), "timeZone": tz},
        "end": {"dateTime": session.end_time.isoformat(timespec="seconds"), "timeZone": tz},
        "colorId": COLOR_BY_BAND[band],
        "extendedProperties": {"private": private},
    }
    body["extendedProperties"]["private"]["content_hash"] = content_hash(body)
    return body
