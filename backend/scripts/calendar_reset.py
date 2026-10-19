"""Delete every event this app created in the sandbox calendar (tagged app=training-optimiser and env=APP_ENV).

Run this after re-seeding the database, because session ids restart and old events would be orphaned.

    cd backend
    python -m scripts.calendar_reset            # asks you to type DELETE
    python -m scripts.calendar_reset --yes      # no question (for scripts)

Events without our tags are never touched. Uses the provider in backend/.env (fake = nothing to delete).
"""
import argparse
import sys

from app.calendar.factory import get_backend
from app.calendar.ops import reset_calendar
from app.config import settings
from app.database import SessionLocal


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--yes", action="store_true", help="skip the confirmation question")
    args = parser.parse_args()

    backend = get_backend()
    print(f"Provider: {backend.provider}   Calendar: {backend.calendar_id}   Environment tag: {settings.app_env}")
    if backend.provider != "google":
        print("The provider is not google, so there is no real calendar to clean. Nothing to do.")
        return 0
    if not args.yes and input("Type DELETE to remove every event this app created in that calendar: ").strip() != "DELETE":
        print("Cancelled. Nothing was deleted.")
        return 1
    with SessionLocal() as db:
        result = reset_calendar(db, backend, "DELETE")
    print(f"Deleted {result['events_deleted']} event(s) and cleared {result['links_cleared']} link(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
