"""Operations around the sync worker: status, retry, backfill, reconcile (read only) and reset."""
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.calendar.base import APP_TAG, CalendarError, Gone, NotFound
from app.calendar.factory import CalendarBackend
from app.calendar.mapping import build_event, content_hash, normalize_remote
from app.calendar.sync import enqueue, utcnow
from app.config import settings
from app.models import CalendarLink, CalendarOutbox, TrainingSession
from app.services.audit import log_event
from app.services.errors import Invalid, NotFound as ApiNotFound
from app.services.sim_clock import get_sim_now

OUTBOX_STATUSES = ("pending", "in_progress", "done", "failed", "skipped")
MAX_BACKFILL_DAYS = 56  # 8 weeks


def row_to_dict(r: CalendarOutbox) -> dict:
    return {
        "id": r.id, "session_id": r.session_id, "plan_id": r.plan_id, "alert_id": r.alert_id, "op": r.op,
        "source": r.source, "change_type": r.change_type, "status": r.status, "attempts": r.attempts,
        "next_attempt_at": r.next_attempt_at, "last_error": r.last_error, "created_at": r.created_at,
        "completed_at": r.completed_at,
    }


def status_summary(db: Session, backend: CalendarBackend) -> dict:
    counts = {s: 0 for s in OUTBOX_STATUSES}
    counts.update(dict(db.execute(select(CalendarOutbox.status, func.count()).group_by(CalendarOutbox.status)).all()))
    last_ok = db.scalar(select(func.max(CalendarOutbox.completed_at)).where(CalendarOutbox.status == "done"))
    recent = db.scalars(select(CalendarOutbox).order_by(CalendarOutbox.id.desc()).limit(20))
    links = dict(db.execute(select(CalendarLink.sync_status, func.count()).group_by(CalendarLink.sync_status)).all())
    return {"provider": backend.provider, "calendar_id": backend.calendar_id, "enabled": settings.calendar_sync_enabled,
            "counts": counts, "links": links, "last_success_at": last_ok, "recent": [row_to_dict(r) for r in recent]}


def failed_rows(db: Session) -> list[dict]:
    return [row_to_dict(r) for r in db.scalars(select(CalendarOutbox).where(CalendarOutbox.status == "failed").order_by(CalendarOutbox.id.desc()))]


def _reset_row(r: CalendarOutbox) -> None:
    r.status, r.attempts, r.next_attempt_at, r.claimed_at, r.completed_at, r.last_error = "pending", 0, None, None, None, None


def retry_one(db: Session, outbox_id: int) -> dict:
    row = db.get(CalendarOutbox, outbox_id)
    if row is None:
        raise ApiNotFound(f"Outbox row {outbox_id} not found")
    _reset_row(row)
    db.commit()
    return row_to_dict(row)


def retry_all_failed(db: Session) -> int:
    rows = list(db.scalars(select(CalendarOutbox).where(CalendarOutbox.status == "failed")))
    for r in rows:
        _reset_row(r)
    db.commit()
    return len(rows)


def enqueue_session(db: Session, session_id: int) -> dict:
    if db.get(TrainingSession, session_id) is None:
        raise ApiNotFound(f"Session {session_id} not found")
    row = enqueue(db, session_id, source="manual")
    db.commit()
    return row_to_dict(row)


def backfill(db: Session, from_date: datetime, to_date: datetime) -> dict:
    if to_date <= from_date:
        raise Invalid("to_date must be after from_date", {"to_date": "must be after from_date"})
    if to_date - from_date > timedelta(days=MAX_BACKFILL_DAYS):
        raise Invalid("A backfill covers at most 8 weeks per call", {"to_date": "window is longer than 8 weeks"})
    ids = list(db.scalars(select(TrainingSession.id).where(
        TrainingSession.status == "scheduled", TrainingSession.start_time >= from_date, TrainingSession.start_time < to_date
    ).order_by(TrainingSession.start_time)))
    for session_id in ids:
        enqueue(db, session_id, source="backfill", change_type="backfill")
    db.commit()
    return {"enqueued": len(ids), "session_ids": ids}


# --- Reconcile ----------------------------------------------------------------------------------------------
def reconcile(db: Session, backend: CalendarBackend, from_dt: datetime, to_dt: datetime) -> dict:
    """Compare Google with the DB for a window. Read only for Google; mismatches are marked ``drift`` on the link."""
    remote = backend.client.list_app_events(backend.calendar_id, from_dt, to_dt, env=settings.app_env)
    by_session: dict[int, dict] = {}
    orphaned = []
    active_ids = set(db.scalars(select(TrainingSession.id).where(TrainingSession.status != "cancelled")))
    for event in remote:
        private = event.get("extendedProperties", {}).get("private", {})
        try:
            sid = int(private.get("session_id", ""))
        except ValueError:
            sid = None
        if sid is None or sid not in active_ids:
            orphaned.append({"event_id": event.get("id"), "session_id": sid, "summary": event.get("summary")})
        else:
            by_session[sid] = event

    links = {l.session_id: l for l in db.scalars(select(CalendarLink).join(
        TrainingSession, TrainingSession.id == CalendarLink.session_id
    ).where(TrainingSession.start_time >= from_dt, TrainingSession.start_time < to_dt, TrainingSession.status != "cancelled",
            CalendarLink.sync_status != "cancelled"))}
    missing, mismatched, in_sync = [], [], 0
    for sid, link in links.items():
        event = by_session.get(sid)
        if event is None:
            missing.append({"session_id": sid, "event_id": link.google_event_id})
            continue
        session = db.get(TrainingSession, sid)
        expected = build_event(session, db)["extendedProperties"]["private"]["content_hash"]
        actual = content_hash(normalize_remote(event))
        if actual != expected:
            mismatched.append({"session_id": sid, "event_id": event.get("id"), "expected_hash": expected, "actual_hash": actual,
                               "synced_with_hash": link.synced_hash})
            if link.sync_status == "synced":
                link.sync_status = "drift"
        else:
            in_sync += 1
    # Remote events for sessions that have no link at all are still tracked (e.g. link lost): count as in sync if equal.
    if mismatched:
        log_event(db, get_sim_now(db), "system", "calendar.drift_detected", "calendar", None, {
            "source": "reconcile", "sessions": [m["session_id"] for m in mismatched]})
    db.commit()
    return {"from": from_dt, "to": to_dt, "remote_events": len(remote), "in_sync": in_sync,
            "missing_in_google": missing, "orphaned_in_google": orphaned, "content_mismatch": mismatched}


# --- Reset --------------------------------------------------------------------------------------------------
def reset_calendar(db: Session, backend: CalendarBackend, confirm: str) -> dict:
    """Delete every event carrying our app + env tags and forget the links. Events without our tags are never touched."""
    if confirm != "DELETE":
        raise Invalid('Send {"confirm": "DELETE"} to reset the calendar', {"confirm": 'must be "DELETE"'})
    events = backend.client.list_app_events(backend.calendar_id, None, None, env=settings.app_env)
    deleted = 0
    for event in events:
        private = event.get("extendedProperties", {}).get("private", {})
        if private.get("app") != APP_TAG or private.get("env") != settings.app_env:
            continue  # belt and braces: the list is already filtered
        try:
            backend.client.delete_event(backend.calendar_id, event["id"])
            deleted += 1
        except (NotFound, Gone):
            pass
    links = list(db.scalars(select(CalendarLink)))
    for link in links:
        db.delete(link)
    log_event(db, get_sim_now(db), "user", "calendar.reset", "calendar", None, {
        "events_deleted": deleted, "links_cleared": len(links), "provider": backend.provider, "env": settings.app_env})
    db.commit()
    return {"events_deleted": deleted, "links_cleared": len(links)}


# --- What the rest of the API shows about a session's sync state ------------------------------------------------
def calendar_info(db: Session, session_ids: list[int]) -> dict[int, dict]:
    """{session_id: {status, html_link, last_synced_at}} for a batch of sessions in two queries.

    status: synced / pending / failed / cancelled / drift, or "none" when the session was never queued.
    """
    ids = list(set(session_ids))
    if not ids:
        return {}
    links = {l.session_id: l for l in db.scalars(select(CalendarLink).where(CalendarLink.session_id.in_(ids)))}
    queued = set(db.scalars(select(CalendarOutbox.session_id).where(
        CalendarOutbox.session_id.in_(ids), CalendarOutbox.status.in_(("pending", "in_progress")))))
    out = {}
    for sid in ids:
        link = links.get(sid)
        status = link.sync_status if link else "none"
        if sid in queued and status in ("none", "synced", "drift"):
            status = "pending"  # a newer change is on its way
        out[sid] = {"status": status, "html_link": link.html_link if link else None,
                    "last_synced_at": link.last_synced_at if link else None}
    return out


def plan_sessions(db: Session, plan_id: int) -> list[dict]:
    """The sessions an approved plan touched, with their calendar status (for the alert page)."""
    ids = sorted(set(db.scalars(select(CalendarOutbox.session_id).where(
        CalendarOutbox.plan_id == plan_id, CalendarOutbox.session_id.is_not(None)))))
    info = calendar_info(db, ids)
    out = []
    for sid in ids:
        s = db.get(TrainingSession, sid)
        out.append({"session_id": sid, "start": s.start_time, "end": s.end_time, "status": s.status, "calendar": info[sid]})
    return out
