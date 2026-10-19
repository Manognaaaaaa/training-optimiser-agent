"""The outbox worker: pushes the current state of changed sessions to Google Calendar, one way (DB -> Google).

Rules that keep it safe:
* It syncs the session's *current DB state*, not a replay of each change, so pending rows for one session are
  coalesced into the newest one (the older ones become ``skipped``).
* Event ids are deterministic, so a crash between "insert" and "save the link" is healed by ``AlreadyExists``.
* A body hash is stored in ``calendar_links``; an unchanged session makes no API call.
* Edits made in Google are drift: reported in the audit log, then overwritten (the DB wins).
"""
import asyncio
import logging
import random
import threading
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.calendar.base import (
    AlreadyExists, CalendarError, Gone, NotFound, PermanentError, PreconditionFailed, RETRYABLE,
)
from app.calendar.factory import CalendarBackend, get_backend
from app.calendar.mapping import build_event, event_id_for
from app.config import settings
from app.models import CalendarLink, CalendarOutbox, TrainingSession
from app.services.audit import log_event
from app.services.sim_clock import get_sim_now

log = logging.getLogger(__name__)
STUCK_AFTER = timedelta(minutes=2)
BACKOFF_CAP_SECONDS = 60

def _publish_update(session_id: int, status: str) -> None:
    from app.live.bus import bus

    bus.publish("calendar.sync_updated", {"session_id": session_id, "status": status})


# Called after the DB commit of every change of a session's sync status. Tests may replace it.
on_update: Callable[[int, str], None] = _publish_update


def utcnow() -> datetime:
    """Real clock, naive UTC. Only the sync machinery uses it; the rest of the app runs on simulated time."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Stats:
    """Counters for the smoke report. Reset with ``stats.reset()``."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        self.api_calls = self.skipped_by_hash = self.synced = self.retries = self.failures = self.coalesced = 0
        self.drift = 0
        self.approve_to_synced_seconds: list[float] = []

    def bump(self, name: str, n: int = 1) -> None:
        with self._lock:
            setattr(self, name, getattr(self, name) + n)


stats = Stats()


class CountingClient:
    """Wraps a client and counts every API call."""

    def __init__(self, inner) -> None:
        self._inner = inner

    def __getattr__(self, name):
        target = getattr(self._inner, name)
        if not callable(target):
            return target

        def counted(*args, **kwargs):
            stats.bump("api_calls")
            return target(*args, **kwargs)

        return counted


# --- Backoff ------------------------------------------------------------------------------------------------
def backoff_seconds(attempts: int) -> float:
    """2s, 4s, 8s, 16s... capped at 60s, with +-20% jitter. ``attempts`` counts the attempt that just failed."""
    base = min(BACKOFF_CAP_SECONDS, 2 ** max(attempts, 1))
    return base * random.uniform(0.8, 1.2)


# --- Queue --------------------------------------------------------------------------------------------------
def recover_stuck(db: Session, now: datetime | None = None) -> int:
    """Rows left ``in_progress`` by a crash go back to ``pending``."""
    now = now or utcnow()
    result = db.execute(update(CalendarOutbox).where(
        CalendarOutbox.status == "in_progress", CalendarOutbox.claimed_at < now - STUCK_AFTER
    ).values(status="pending", claimed_at=None))
    db.commit()
    return result.rowcount or 0


def claim_next(db: Session, now: datetime) -> CalendarOutbox | None:
    """Claim the newest pending row of the oldest due session, and mark that session's older pending rows skipped."""
    due = db.scalars(select(CalendarOutbox).where(
        CalendarOutbox.status == "pending",
        (CalendarOutbox.next_attempt_at.is_(None)) | (CalendarOutbox.next_attempt_at <= now),
    ).order_by(CalendarOutbox.id).limit(1)).first()
    if due is None:
        return None
    newest = db.scalars(select(CalendarOutbox).where(
        CalendarOutbox.status == "pending", CalendarOutbox.session_id == due.session_id
    ).order_by(CalendarOutbox.id.desc()).limit(1)).first() if due.session_id is not None else due
    claimed = db.execute(update(CalendarOutbox).where(
        CalendarOutbox.id == newest.id, CalendarOutbox.status == "pending"
    ).values(status="in_progress", claimed_at=now))
    if not claimed.rowcount:
        db.rollback()
        return None  # somebody else got it
    if newest.session_id is not None:
        skipped = db.execute(update(CalendarOutbox).where(
            CalendarOutbox.session_id == newest.session_id, CalendarOutbox.status == "pending", CalendarOutbox.id < newest.id
        ).values(status="skipped", completed_at=now))
        stats.bump("coalesced", skipped.rowcount or 0)
    db.commit()
    db.refresh(newest)
    return newest


def enqueue(
    db: Session, session_id: int, *, source: str, op: str = "upsert", plan_id: int | None = None,
    alert_id: int | None = None, change_type: str = "manual",
) -> CalendarOutbox:
    """Queue one session for syncing (the caller commits)."""
    row = CalendarOutbox(
        session_id=session_id, op=op, source=source, plan_id=plan_id, alert_id=alert_id, change_type=change_type,
        payload={}, created_at=get_sim_now(db))
    db.add(row)
    db.flush()
    return row


def enqueue_if_linked(db: Session, session_id: int, op: str = "upsert") -> None:
    """A manual edit to a session already in Google queues a re-sync. Sessions never synced are left alone."""
    if db.scalar(select(CalendarLink.id).where(CalendarLink.session_id == session_id, CalendarLink.sync_status != "cancelled")):
        enqueue(db, session_id, source="manual", op=op)


# --- Syncing one session ----------------------------------------------------------------------------------
def _get_link(db: Session, session_id: int) -> CalendarLink | None:
    return db.scalar(select(CalendarLink).where(CalendarLink.session_id == session_id))


def _save_link(db: Session, link: CalendarLink | None, session_id: int, backend: CalendarBackend, status: str,
               event: dict | None = None, digest: str | None = None, error: str | None = None) -> CalendarLink:
    if link is None:
        link = CalendarLink(session_id=session_id, google_event_id=event_id_for(session_id), calendar_id=backend.calendar_id)
        db.add(link)
    link.sync_status = status
    link.last_error = error
    if event is not None:
        link.google_event_id = event.get("id", link.google_event_id)
        link.etag = event.get("etag")
        link.html_link = event.get("htmlLink")
    if digest is not None:
        link.synced_hash = digest
    if status in ("synced", "cancelled"):
        link.last_synced_at = utcnow()
    return link


def _insert_or_adopt(client, calendar_id: str, event_id: str, body: dict) -> dict:
    """Insert; if the id is taken (earlier crash, or a deleted event), adopt it by updating."""
    try:
        return client.insert_event(calendar_id, event_id, body)
    except AlreadyExists:
        try:
            existing = client.get_event(calendar_id, event_id)
            return client.update_event(calendar_id, event_id, body, etag=existing.get("etag"))
        except (NotFound, Gone):
            return client.update_event(calendar_id, event_id, body, etag=None)  # restores a deleted event


def _upsert(db: Session, backend: CalendarBackend, session: TrainingSession, row: CalendarOutbox) -> str:
    """Returns "synced" or "unchanged"."""
    client, cal = backend.client, backend.calendar_id
    body = build_event(session, db)
    digest = body["extendedProperties"]["private"]["content_hash"]
    link = _get_link(db, session.id)
    if link is not None and link.sync_status == "synced" and link.synced_hash == digest and link.calendar_id == cal:
        return "unchanged"
    if link is None:
        event = _insert_or_adopt(client, cal, event_id_for(session.id), body)
    else:
        try:
            event = client.update_event(cal, link.google_event_id, body, etag=link.etag)
        except PreconditionFailed:  # edited in Google since our last push: report it, then DB wins
            stats.bump("drift")
            log_event(db, get_sim_now(db), "system", "calendar.drift_detected", "training_session", session.id,
                      {"session_id": session.id, "outbox_id": row.id, "event_id": link.google_event_id})
            event = client.update_event(cal, link.google_event_id, body, etag=None)
        except (NotFound, Gone):  # deleted in Google: put it back
            event = _insert_or_adopt(client, cal, link.google_event_id, body)
    _save_link(db, link, session.id, backend, "synced", event, digest)
    return "synced"


def _cancel(db: Session, backend: CalendarBackend, session_id: int) -> str:
    link = _get_link(db, session_id)
    event_id = link.google_event_id if link else event_id_for(session_id)
    try:
        backend.client.delete_event(backend.calendar_id, event_id)
    except (NotFound, Gone):
        pass  # already gone: that is what we wanted
    if link is not None:
        _save_link(db, link, session_id, backend, "cancelled")
    return "cancelled"


def process_row(db: Session, backend: CalendarBackend, row: CalendarOutbox) -> str:
    """Do the work of one claimed row and record the outcome. Returns the link status or "failed"/"retry"."""
    now_real = utcnow()
    session = db.get(TrainingSession, row.session_id) if row.session_id is not None else None
    sim_now = get_sim_now(db)
    try:
        if row.session_id is None:
            raise PermanentError("outbox row has no session_id")
        cancelling = row.op == "cancel" or session is None or session.status == "cancelled"
        result = _cancel(db, backend, row.session_id) if cancelling else _upsert(db, backend, session, row)
    except CalendarError as err:
        return _record_failure(db, backend, row, err, now_real, sim_now)
    except Exception as err:  # a bug: do not retry forever
        return _record_failure(db, backend, row, PermanentError(f"{err.__class__.__name__}: {err}"), now_real, sim_now)

    row.status, row.completed_at, row.last_error = "done", now_real, None
    row.attempts += 1
    if result == "unchanged":
        stats.bump("skipped_by_hash")
    else:
        stats.bump("synced")
    if row.created_real_at:
        stats.approve_to_synced_seconds.append((now_real - row.created_real_at).total_seconds())
    log_event(db, sim_now, "system", "calendar.sync_ok", "training_session", row.session_id, {
        "session_id": row.session_id, "plan_id": row.plan_id, "op": row.op, "result": result, "attempts": row.attempts})
    status = result if result != "unchanged" else "synced"
    db.commit()
    on_update(row.session_id, status)
    return status


def _record_failure(db: Session, backend: CalendarBackend, row: CalendarOutbox, err: CalendarError,
                    now_real: datetime, sim_now: datetime) -> str:
    db.rollback()  # throw away anything half-written for this session
    row = db.get(CalendarOutbox, row.id)
    row.attempts += 1
    row.last_error = f"{err.__class__.__name__}: {err}"
    retry = isinstance(err, RETRYABLE) and row.attempts < settings.calendar_max_attempts
    if retry:
        row.status, row.claimed_at = "pending", None
        row.next_attempt_at = now_real + timedelta(seconds=backoff_seconds(row.attempts))
        stats.bump("retries")
        outcome = "retry"
    else:
        row.status, row.completed_at = "failed", now_real
        stats.bump("failures")
        outcome = "failed"
        if row.session_id is not None:
            _save_link(db, _get_link(db, row.session_id), row.session_id, backend, "failed", error=row.last_error)
    log_event(db, sim_now, "system", "calendar.sync_failed", "training_session", row.session_id, {
        "session_id": row.session_id, "plan_id": row.plan_id, "op": row.op, "attempts": row.attempts,
        "error": row.last_error, "will_retry": retry})
    db.commit()
    if row.session_id is not None:
        on_update(row.session_id, "pending" if retry else "failed")
    return outcome


def process_due(db: Session, backend: CalendarBackend | None = None, max_rows: int = 50) -> int:
    """Claim and process due rows one at a time (sequential keeps us under Google's quotas). Returns rows handled."""
    backend = backend or get_backend()
    wrapped = CalendarBackend(CountingClient(backend.client), backend.provider, backend.calendar_id)
    handled = 0
    while handled < max_rows:
        row = claim_next(db, utcnow())
        if row is None:
            break
        process_row(db, wrapped, row)
        handled += 1
    return handled


# --- The background worker -------------------------------------------------------------------------------------
class SyncService:
    """One asyncio task started in the FastAPI lifespan. ``kick()`` wakes it right away (safe from any thread)."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake: asyncio.Event | None = None
        self._task: asyncio.Task | None = None
        self.session_factory: Callable[[], Session] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self, session_factory: Callable[[], Session]) -> None:
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        self.session_factory = session_factory
        with session_factory() as db:
            recover_stuck(db)
        self._task = asyncio.create_task(self._run(), name="calendar-sync")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = self._loop = self._wake = None

    def kick(self) -> None:
        if self._loop is not None and self._wake is not None:
            self._loop.call_soon_threadsafe(self._wake.set)

    def _tick(self) -> int:
        with self.session_factory() as db:
            return process_due(db)

    async def _run(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=settings.calendar_sync_interval_seconds)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            try:
                await asyncio.to_thread(self._tick)  # the Google client blocks
            except Exception:  # never let the worker die
                log.exception("calendar sync tick failed")


sync_service = SyncService()
