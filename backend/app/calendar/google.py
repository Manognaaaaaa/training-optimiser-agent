"""Google Calendar client using a service account (no OAuth consent screen).

The Google libraries are imported lazily so the app and the tests run without credentials.
"""
import json
from datetime import datetime, timezone

from app.calendar.base import (
    APP_TAG, AlreadyExists, CalendarError, Gone, NotFound, PermanentError, PreconditionFailed, RateLimited,
    TransientError,
)

SCOPES = ["https://www.googleapis.com/auth/calendar"]
RATE_LIMIT_REASONS = {"rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded"}


def map_http_error(status: int, reason: str, message: str) -> CalendarError:
    """Turn an HTTP status (and Google's error reason) into one of our typed errors."""
    text = f"HTTP {status} {reason}: {message}".strip()
    if status == 404:
        return NotFound(text)
    if status == 409:
        return AlreadyExists(text)
    if status == 410:
        return Gone(text)
    if status == 412:
        return PreconditionFailed(text)
    if status == 429 or (status == 403 and reason in RATE_LIMIT_REASONS):
        return RateLimited(text)
    if status >= 500:
        return TransientError(text)
    return PermanentError(text)


def _utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class GoogleCalendarClient:
    def __init__(self, service_account_file: str, service=None) -> None:
        if service is None:
            from google.oauth2 import service_account
            from googleapiclient.discovery import build

            creds = service_account.Credentials.from_service_account_file(service_account_file, scopes=SCOPES)
            service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        self._events = service.events()

    def _call(self, request):
        """Execute a request and translate every failure into a typed error."""
        from googleapiclient.errors import HttpError

        try:
            return request.execute()
        except HttpError as err:
            reason, message = "", str(err)
            try:
                detail = json.loads(err.content.decode("utf-8"))["error"]
                message = detail.get("message", message)
                reason = (detail.get("errors") or [{}])[0].get("reason", "")
            except (ValueError, KeyError, AttributeError, TypeError):
                pass
            raise map_http_error(int(err.resp.status), reason, message) from err
        except (OSError, TimeoutError) as err:  # network trouble
            raise TransientError(f"{err.__class__.__name__}: {err}") from err

    def insert_event(self, calendar_id: str, event_id: str, body: dict) -> dict:
        return self._call(self._events.insert(calendarId=calendar_id, body={**body, "id": event_id}))

    def update_event(self, calendar_id: str, event_id: str, body: dict, etag: str | None = None) -> dict:
        request = self._events.update(calendarId=calendar_id, eventId=event_id, body=body)
        if etag:
            request.headers["If-Match"] = etag
        return self._call(request)

    def get_event(self, calendar_id: str, event_id: str) -> dict:
        event = self._call(self._events.get(calendarId=calendar_id, eventId=event_id))
        if event.get("status") == "cancelled":  # Google keeps deleted events around as "cancelled"
            raise Gone(event_id)
        return event

    def delete_event(self, calendar_id: str, event_id: str) -> None:
        self._call(self._events.delete(calendarId=calendar_id, eventId=event_id))

    def list_app_events(self, calendar_id: str, time_min: datetime | None = None, time_max: datetime | None = None,
                        env: str | None = None) -> list[dict]:
        props = [f"app={APP_TAG}"] + ([f"env={env}"] if env else [])
        params: dict = {"calendarId": calendar_id, "privateExtendedProperty": props, "singleEvents": True,
                        "maxResults": 250, "showDeleted": False}
        if time_min:
            params["timeMin"] = _utc(time_min)
        if time_max:
            params["timeMax"] = _utc(time_max)
        out: list[dict] = []
        while True:
            page = self._call(self._events.list(**params))
            out.extend(page.get("items", []))
            token = page.get("nextPageToken")
            if not token:
                return out
            params["pageToken"] = token
