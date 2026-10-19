"""Pick the calendar client from config. A broken Google setup falls back to the fake, with a warning."""
import logging
import os
from dataclasses import dataclass

from app.calendar.base import CalendarClient
from app.calendar.fake import FakeCalendarClient
from app.config import Settings, settings as default_settings

log = logging.getLogger(__name__)


@dataclass
class CalendarBackend:
    client: CalendarClient
    provider: str  # "fake" | "google"
    calendar_id: str


_backend: CalendarBackend | None = None


def build_backend(cfg: Settings) -> CalendarBackend:
    if cfg.calendar_provider == "google":
        if not cfg.google_service_account_file or not os.path.isfile(cfg.google_service_account_file) or not cfg.google_calendar_id:
            log.warning("CALENDAR_PROVIDER=google but GOOGLE_SERVICE_ACCOUNT_FILE or GOOGLE_CALENDAR_ID is missing or "
                        "the key file does not exist; falling back to the fake in-memory calendar")
        else:
            try:
                from app.calendar.google import GoogleCalendarClient

                return CalendarBackend(GoogleCalendarClient(cfg.google_service_account_file), "google", cfg.google_calendar_id)
            except Exception as err:  # bad key file, missing libraries...
                log.warning("Could not start the Google Calendar client (%s); falling back to the fake calendar", err)
    elif cfg.calendar_provider != "fake":
        log.warning("Unknown CALENDAR_PROVIDER %r; using the fake calendar", cfg.calendar_provider)
    return CalendarBackend(FakeCalendarClient(), "fake", cfg.google_calendar_id or "fake-calendar")


def get_backend() -> CalendarBackend:
    """The process-wide backend (created on first use). Tests replace it with ``set_backend``."""
    global _backend
    if _backend is None:
        _backend = build_backend(default_settings)
    return _backend


def set_backend(backend: CalendarBackend | None) -> None:
    global _backend
    _backend = backend
