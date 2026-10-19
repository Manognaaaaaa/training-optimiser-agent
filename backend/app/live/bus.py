"""In-process event bus behind the live updates (Server-Sent Events).

* ``publish(type, payload)`` is safe to call from async code AND from worker threads (the simulator and the planner
  run in threads): delivery always hops onto the event loop with ``call_soon_threadsafe``.
* Every event gets an increasing integer id. The last 200 are kept so a client that reconnects with
  ``Last-Event-ID`` gets what it missed; an id older than the buffer (or from before a server restart) gets ``resync``.
* A subscriber whose queue is full is cleared and sent a single ``resync`` (the client then refetches everything);
  other subscribers are not affected.
* ``publish_after_commit`` holds events until the database transaction really commits, so the UI is never told
  about data that was rolled back.

This bus lives in one process: run uvicorn with ONE worker. Several workers would need Postgres LISTEN/NOTIFY.
"""
import asyncio
import threading
import time
from collections import deque
from dataclasses import dataclass, field

from sqlalchemy import event
from sqlalchemy.orm import Session

BUFFER_SIZE = 200
QUEUE_SIZE = 100
PENDING_KEY = "live_pending_events"


@dataclass
class LiveEvent:
    id: int
    type: str
    data: dict
    ts: float = field(default_factory=time.time)

    def as_dict(self) -> dict:
        return {"id": self.id, "type": self.type, "data": self.data, "ts": self.ts}


class Subscriber:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[LiveEvent] = asyncio.Queue(maxsize=QUEUE_SIZE)
        self.last_id = 0  # events with an id at or below this were already queued for this client


class EventBus:
    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._subs: set[Subscriber] = set()
        self._buffer: deque[LiveEvent] = deque(maxlen=BUFFER_SIZE)
        self._next_id = 1
        self._lock = threading.Lock()
        self.published = 0

    # --- setup -----------------------------------------------------------------------------------------
    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def reset(self) -> None:
        """Forget everything (tests)."""
        with self._lock:
            self._buffer.clear()
            self._next_id = 1
            self._subs.clear()
            self._loop = None
            self.published = 0

    @property
    def latest_id(self) -> int:
        return self._next_id - 1

    # --- publishing ------------------------------------------------------------------------------------
    def publish(self, type: str, payload: dict | None = None) -> LiveEvent:
        with self._lock:
            ev = LiveEvent(self._next_id, type, payload or {})
            self._next_id += 1
            self._buffer.append(ev)
            self.published += 1
        loop = self._loop
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(self._deliver, ev)
            except RuntimeError:  # loop shutting down
                pass
        return ev

    def _deliver(self, ev: LiveEvent) -> None:
        """Runs on the event loop."""
        for sub in list(self._subs):
            self._offer(sub, ev)

    def _offer(self, sub: Subscriber, ev: LiveEvent) -> None:
        if ev.id <= sub.last_id:
            return  # already sent through the replay
        try:
            sub.queue.put_nowait(ev)
            sub.last_id = ev.id
        except asyncio.QueueFull:
            while not sub.queue.empty():  # drop what it could not keep up with
                sub.queue.get_nowait()
            sub.queue.put_nowait(LiveEvent(ev.id, "resync", {"reason": "overflow"}))
            sub.last_id = ev.id

    # --- subscribing -----------------------------------------------------------------------------------
    def subscribe(self, last_event_id: int | None = None) -> Subscriber:
        """Call on the event loop. Replays missed events when ``last_event_id`` is still in the buffer."""
        sub = Subscriber()
        with self._lock:
            buffered = list(self._buffer)
            latest = self._next_id - 1
        if last_event_id is not None:
            oldest = buffered[0].id if buffered else latest + 1
            if last_event_id > latest or last_event_id < oldest - 1:
                reason = "server_restarted" if last_event_id > latest else "stale_last_event_id"
                self._offer(sub, LiveEvent(latest, "resync", {"reason": reason}))
            else:
                sub.last_id = last_event_id
                for ev in buffered:
                    if ev.id > last_event_id:
                        self._offer(sub, ev)
        else:
            sub.last_id = latest
        self._subs.add(sub)
        return sub

    def unsubscribe(self, sub: Subscriber) -> None:
        self._subs.discard(sub)

    @property
    def subscriber_count(self) -> int:
        return len(self._subs)


bus = EventBus()

# Event types whose list fields are merged (union) when several are queued in one transaction.
MERGEABLE = {"sessions.changed", "forecast.updated", "attendance.recorded"}


def publish_after_commit(db: Session, type: str, payload: dict | None = None) -> None:
    """Queue an event on the DB session; it is published when (and only if) the transaction commits."""
    pending: list[tuple[str, dict]] = db.info.setdefault(PENDING_KEY, [])
    payload = payload or {}
    if type in MERGEABLE:
        for i, (t, existing) in enumerate(pending):
            if t == type:
                merged = dict(existing)
                for key, value in payload.items():
                    if isinstance(value, list):
                        merged[key] = sorted(set(existing.get(key, [])) | set(value))
                    elif isinstance(value, int) and not isinstance(value, bool):
                        merged[key] = existing.get(key, 0) + value
                    else:
                        merged[key] = value
                pending[i] = (t, merged)
                return
    pending.append((type, payload))


def publish_alert(db: Session, alert, type: str) -> None:
    """Tell the UI (after the commit) that an alert was created or changed."""
    from app.models import Course  # local import: models do not know about the bus

    course = db.get(Course, alert.course_id)
    publish_after_commit(db, type, {
        "alert_id": alert.id, "course_id": alert.course_id, "course_name": course.name if course else None,
        "risk_level": alert.risk_level, "status": alert.status})


@event.listens_for(Session, "after_commit")
def _flush_pending(db: Session) -> None:
    for type, payload in db.info.pop(PENDING_KEY, []):
        bus.publish(type, payload)


@event.listens_for(Session, "after_soft_rollback")
def _drop_pending(db: Session, previous_transaction) -> None:
    db.info.pop(PENDING_KEY, None)
