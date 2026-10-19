# Google Calendar setup (sandbox calendar)

The app can push training sessions to a Google Calendar. It writes to **one sandbox calendar only**, using a
**service account** (a robot Google account), so there is no sign-in screen. Without this setup the app still works:
`CALENDAR_PROVIDER=fake` (the default) keeps a pretend calendar in memory.

## 1. Create a Google Cloud project and turn on the Calendar API

1. Go to <https://console.cloud.google.com/> and create a project, for example `training-optimiser`.
2. Open **APIs & Services > Library**, search for **Google Calendar API** and press **Enable**.

## 2. Create a service account and download its key

1. Open **IAM & Admin > Service Accounts > Create service account**. Name it `training-optimiser-sync`. No roles are needed.
2. Open the new account, go to **Keys > Add key > Create new key > JSON**. A `.json` file downloads.
3. Keep that file **outside the repo** (for example `C:\Users\you\secrets\training-optimiser-sa.json`), or inside the repo only in a
   git-ignored path (`service-account*.json`, `*-credentials.json` and `secrets/` are already ignored). **Never commit it.**
4. Write down the service account's email address (it looks like `training-optimiser-sync@your-project.iam.gserviceaccount.com`).

## 3. Create the sandbox calendar

1. In Google Calendar (a normal account), press **+ next to "Other calendars" > Create new calendar**.
2. Name it **Training Optimiser Sandbox**. **Do not use your personal main calendar.**

## 4. Share it with the service account

1. Open the new calendar's **Settings and sharing**.
2. Under **Share with specific people or groups**, add the service account email with the permission **Make changes to events**.

## 5. Tell the app

1. In the same settings page, copy the **Calendar ID** (under "Integrate calendar"; it looks like `abc123@group.calendar.google.com`).
2. Edit `backend/.env`:

```
CALENDAR_PROVIDER=google
GOOGLE_SERVICE_ACCOUNT_FILE=C:\Users\you\secrets\training-optimiser-sa.json
GOOGLE_CALENDAR_ID=abc123@group.calendar.google.com
CALENDAR_TIMEZONE=Asia/Dubai
APP_ENV=dev
```

`APP_ENV` tags every event this machine creates. If two people share one sandbox calendar, give each a different value
(for example `manogna` and `sam`) so they do not collide and each reset only removes their own events.

3. Restart the backend. If a value is missing the backend logs a warning and falls back to the fake calendar, so the app still runs.

## 6. Check that it works

```powershell
cd backend
.venv\Scripts\Activate.ps1
uvicorn app.main:app            # in one terminal, ONE worker (no --workers)
python -m scripts.smoke_phase7  # in another; writes docs/calendar_sync_report.md and asks before cleaning up
```

You can also open the **Simulation** page and use **Google Calendar sync** (send a week of sessions, check for differences, reset).

## After re-seeding the database

Session ids restart when the database is re-seeded, so old events would be orphans. The **Reset** button on the Simulation page already
clears the calendar first. If you re-seed from the command line, run `python -m scripts.calendar_reset` yourself.

## Limitations

- **No attendee invites.** A service account without domain-wide delegation cannot invite people, so the roster is written in the event description.
- **One-way sync.** The database is the source of truth. Edits made in Google Calendar are detected (drift) and overwritten on the next sync;
  they are never copied back into the app.
- **Single process.** The live-update bus lives inside one process: run uvicorn with one worker.
- Only events tagged `app=training-optimiser` and `env=<APP_ENV>` are ever changed or deleted. Anything else in the calendar is left alone.
