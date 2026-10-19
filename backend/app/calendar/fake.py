"""In-memory calendar for tests and for running the app without Google."""
import copy
import threading
from datetime import datetime

from app.calendar.base import (
    APP_TAG, AlreadyExists, CalendarError, Gone, NotFound, PreconditionFailed,
)


class FakeCalendarClient:
    """Behaves like the Google client. Records every call and can be told to fail on purpose."""

    def __init__(self) -> None:
        self.events: dict[tuple[str, str], dict] = {}
        self.deleted: set[tuple[str, str]] = set()
        self.calls: list[tuple] = []  # (method, calendar_id, event_id)
        self._failures: list[tuple[str | None, type[CalendarError], int]] = []  # (method, error, times left)
        self._etag = 0
        self._lock = threading.Lock()

    # --- test helpers ------------------------------------------------------------------------------
    def fail_next(self, times: int, error: type[CalendarError] = CalendarError, method: str | None = None) -> None:
        """Make the next ``times`` calls (of one method, or any) raise ``error``."""
        self._failures.append((method, error, times))

    def calls_to(self, method: str) -> list[tuple]:
        return [c for c in self.calls if c[0] == method]

    def external_edit(self, calendar_id: str, event_id: str, **changes) -> None:
        """Simulate someone editing the event in the Google Calendar UI: the body changes and the etag moves."""
        with self._lock:
            event = self.events[(calendar_id, event_id)]
            event.update(changes)
            event["etag"] = self._next_etag()

    def external_delete(self, calendar_id: str, event_id: str) -> None:
        with self._lock:
            self.events.pop((calendar_id, event_id), None)
            self.deleted.add((calendar_id, event_id))

    def forget(self, calendar_id: str, event_id: str) -> None:
        """Remove an event without a trace (updating it later gives NotFound, not Gone)."""
        with self._lock:
            self.events.pop((calendar_id, event_id), None)

    def put_raw(self, calendar_id: str, event_id: str, body: dict) -> None:
        """Add an event without going through insert (to set up orphans or foreign events)."""
        with self._lock:
            self.events[(calendar_id, event_id)] = self._stamp(calendar_id, event_id, body)

    # --- internals ----------------------------------------------------------------------------------
    def _next_etag(self) -> str:
        self._etag += 1
        return f'"{self._etag}"'

    def _stamp(self, calendar_id: str, event_id: str, body: dict) -> dict:
        event = copy.deepcopy(body)
        event.update({"id": event_id, "etag": self._next_etag(), "status": "confirmed",
                      "htmlLink": f"https://calendar.example/{calendar_id}/{event_id}"})
        return event

    def _enter(self, method: str, calendar_id: str, event_id: str) -> None:
        self.calls.append((method, calendar_id, event_id))
        for i, (m, error, left) in enumerate(self._failures):
            if m in (None, method) and left > 0:
                self._failures[i] = (m, error, left - 1)
                raise error(f"injected {error.__name__} on {method}")

    # --- the client interface -----------------------------------------------------------------------
    def insert_event(self, calendar_id: str, event_id: str, body: dict) -> dict:
        with self._lock:
            self._enter("insert", calendar_id, event_id)
            if (calendar_id, event_id) in self.events or (calendar_id, event_id) in self.deleted:
                raise AlreadyExists(event_id)  # Google keeps deleted ids reserved too
            self.events[(calendar_id, event_id)] = self._stamp(calendar_id, event_id, body)
            return copy.deepcopy(self.events[(calendar_id, event_id)])

    def update_event(self, calendar_id: str, event_id: str, body: dict, etag: str | None = None) -> dict:
        with self._lock:
            self._enter("update", calendar_id, event_id)
            current = self.events.get((calendar_id, event_id))
            if current is None:
                if etag is None and (calendar_id, event_id) in self.deleted:  # Google restores a cancelled event
                    self.deleted.discard((calendar_id, event_id))
                    self.events[(calendar_id, event_id)] = self._stamp(calendar_id, event_id, body)
                    return copy.deepcopy(self.events[(calendar_id, event_id)])
                raise Gone(event_id) if (calendar_id, event_id) in self.deleted else NotFound(event_id)
            if etag is not None and etag != current["etag"]:
                raise PreconditionFailed(event_id)
            self.events[(calendar_id, event_id)] = self._stamp(calendar_id, event_id, body)
            return copy.deepcopy(self.events[(calendar_id, event_id)])

    def get_event(self, calendar_id: str, event_id: str) -> dict:
        with self._lock:
            self._enter("get", calendar_id, event_id)
            current = self.events.get((calendar_id, event_id))
            if current is None:
                raise NotFound(event_id)
            return copy.deepcopy(current)

    def delete_event(self, calendar_id: str, event_id: str) -> None:
        with self._lock:
            self._enter("delete", calendar_id, event_id)
            if (calendar_id, event_id) not in self.events:
                raise Gone(event_id) if (calendar_id, event_id) in self.deleted else NotFound(event_id)
            del self.events[(calendar_id, event_id)]
            self.deleted.add((calendar_id, event_id))

    def list_app_events(self, calendar_id: str, time_min: datetime | None = None, time_max: datetime | None = None,
                        env: str | None = None) -> list[dict]:
        with self._lock:
            self._enter("list", calendar_id, "")
            out = []
            for (cal, _), event in self.events.items():
                private = event.get("extendedProperties", {}).get("private", {})
                if cal != calendar_id or private.get("app") != APP_TAG:
                    continue
                if env is not None and private.get("env") != env:
                    continue
                start = event.get("start", {}).get("dateTime")
                if start and (time_min or time_max):
                    when = datetime.fromisoformat(start).replace(tzinfo=None)
                    if (time_min and when < time_min) or (time_max and when >= time_max):
                        continue
                out.append(copy.deepcopy(event))
            return out
