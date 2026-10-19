"""The calendar client interface and its typed errors.

Everything that talks to a calendar goes through ``CalendarClient``. There is a fake (tests, default) and a
Google implementation. Events are plain dicts shaped like the Google Calendar API's ``events`` resource, plus
the server-assigned ``id``, ``etag`` and ``htmlLink``.
"""
from datetime import datetime
from typing import Protocol

APP_TAG = "training-optimiser"  # extendedProperties.private.app on every event we create


class CalendarError(Exception):
    """Base class. ``str(error)`` is stored in ``last_error``."""


class NotFound(CalendarError): ...           # 404
class AlreadyExists(CalendarError): ...      # 409: an event with this id exists
class Gone(CalendarError): ...               # 410: the event was deleted
class PreconditionFailed(CalendarError): ...  # 412: the etag no longer matches (edited elsewhere)
class RateLimited(CalendarError): ...        # 403 rateLimitExceeded / 429
class TransientError(CalendarError): ...     # 5xx, network problems
class PermanentError(CalendarError): ...     # other 4xx: retrying will not help


RETRYABLE = (RateLimited, TransientError)


class CalendarClient(Protocol):
    def insert_event(self, calendar_id: str, event_id: str, body: dict) -> dict: ...

    def update_event(self, calendar_id: str, event_id: str, body: dict, etag: str | None = None) -> dict: ...

    def get_event(self, calendar_id: str, event_id: str) -> dict: ...

    def delete_event(self, calendar_id: str, event_id: str) -> None: ...

    def list_app_events(
        self, calendar_id: str, time_min: datetime | None = None, time_max: datetime | None = None, env: str | None = None
    ) -> list[dict]:
        """Events carrying our ``app`` tag (and ``env`` tag if given) that start inside the window."""
        ...
