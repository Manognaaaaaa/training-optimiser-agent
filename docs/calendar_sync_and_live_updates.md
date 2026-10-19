# Calendar sync and live updates (Phase 7)

```
approve plan ──► calendar_outbox ──► sync worker ──► Google Calendar        (DB → Google, one way)
any change ──► DB commit ──► event bus ──► SSE stream ──► react-query       (DB → bus → browser)
```

The LLM is not involved anywhere in this phase. The constraint checker, scorer and planner are unchanged.

## Calendar sync

### Tables
- `calendar_outbox`: one row per session that needs syncing. Columns: `session_id`, `op` (`upsert` / `cancel`), `source`
  (`plan_approval` / `manual` / `backfill`), `plan_id`, `alert_id`, `status` (`pending` → `in_progress` → `done` / `failed` / `skipped`),
  `attempts`, `next_attempt_at`, `last_error`, `claimed_at`, `created_at` (sim time), `completed_at`. A plan's `move_driver` writes two rows
  (the from-session and the to-session). The worker timestamps are real UTC; everything else in the app uses simulated time.
- `calendar_links`: one row per session that exists in Google: `google_event_id`, `etag`, `html_link`, `synced_hash`, `sync_status`
  (`synced` / `pending` / `failed` / `cancelled` / `drift`).

### Event ids
`"tos" + base32hex(sha1("{APP_ENV}:{session_id}"))`, lowercase, at most 40 characters. Google ids may only contain `a-v` and `0-9`, which is
exactly the base32hex alphabet. The same session always gets the same id, so a retry can never make a duplicate.

### What an event contains
Summary `CODE: Course name` (plus ` (added by plan)`), start and end in `CALENDAR_TIMEZONE`, location, a description (trainer, capacity,
enrolled, fill rate, roster, and for plan sessions the plan, alert, rank, reason and a link to `/alerts/{id}`), a colour by fill rate
(10 green ≥ 80%, 5 yellow 50–79%, 11 red < 50%), and private extended properties `app`, `env`, `session_id`, `plan_id`, `content_hash`.
No attendees (see the limitations in the setup guide).

### Worker rules
- **Coalescing.** The worker syncs the session's current state. When it claims a row, older pending rows for the same session become `skipped`.
- **Hash skip.** If the link is `synced` and the stored hash equals the new body's hash, no API call is made.
- **Insert / adopt.** No link → insert. `AlreadyExists` (a crash between insert and save, or a deleted id) → get + update.
- **Updates** send the stored etag. Cancelled sessions are deleted from Google.
- **Claiming** is one `UPDATE … WHERE status='pending'`. Rows stuck `in_progress` for more than 2 minutes go back to `pending` when the worker starts.
- Runs as one asyncio task (`CALENDAR_SYNC_INTERVAL_SECONDS`), processing rows one at a time through `asyncio.to_thread`. Approve calls `kick()` to wake it.

### Errors and what happens
| Error | Meaning | Action |
|---|---|---|
| `RateLimited` (429, 403 rate-limit reasons) | slow down | retry with backoff 2 s, 4 s, 8 s, 16 s, cap 60 s (±20% jitter) |
| `TransientError` (5xx, network) | temporary | same retry; after `CALENDAR_MAX_ATTEMPTS` the row is `failed` |
| `PermanentError` (other 4xx) | will not work | `failed` at once, link `failed` |
| `AlreadyExists` on insert | event with our id exists | get + update (adopt it) |
| `PreconditionFailed` (412) on update | edited in Google | audit `calendar.drift_detected`, overwrite (DB wins) |
| `NotFound` / `Gone` on update | deleted in Google | insert again (or restore it) |
| `NotFound` / `Gone` on cancel | already gone | success |

Every outcome writes an audit row (`calendar.sync_ok` / `calendar.sync_failed`); drift writes `calendar.drift_detected`; a reset writes `calendar.reset`.

### Drift and reconcile
`GET /api/calendar/reconcile?from=&to=` lists our tagged events in Google for the window and compares them with the database: **missing in
Google**, **orphaned** (no active session), **content mismatch** (the event, normalised and re-hashed, differs from what the DB would produce),
and the in-sync count. It never changes Google. Mismatches mark the link `drift`, which makes the next sync of that session push again (the hash skip
only applies to `synced` links). Edits are never copied back to the database.

### When a session is queued
Plan approval (all touched sessions), `POST /api/calendar/sync/session/{id}`, `POST /api/calendar/backfill` (max 8 weeks), and manual edits,
cancels or enrolment changes to a session that is already linked. Simulated attendance does not queue anything (it would flood the calendar);
reconcile reports the roster difference as a mismatch.

### API
`GET /api/calendar/sync/status`, `GET /api/calendar/sync/failed`, `POST /api/calendar/sync/retry/{id}`, `POST /api/calendar/sync/retry-all-failed`,
`POST /api/calendar/sync/session/{id}`, `POST /api/calendar/backfill`, `GET /api/calendar/reconcile`, `POST /api/calendar/reset {"confirm":"DELETE"}`.
Session payloads (`/api/sessions`, `/api/calendar`, alert detail for an approved plan) carry `calendar: {status, html_link, last_synced_at}`.

## Live updates

`GET /api/live/stream` is a Server-Sent Events stream (`text/event-stream`, `retry: 3000`, heartbeat every 15 s). Each event has an increasing
`id`; the last 200 are kept, so a browser that reconnects with `Last-Event-ID` receives what it missed. An id older than the buffer (or from before
a restart) gets `resync`, and so does a subscriber whose queue overflowed. Events are published **only after the database commit**
(`publish_after_commit`), so the UI is never told about rolled-back data. `publish` works from async code and from worker threads
(the simulator and planner run in threads) by hopping onto the event loop with `call_soon_threadsafe`.

| type | payload | published from |
|---|---|---|
| `sim.advanced` | sim time, days advanced | simulator advance (once per call) |
| `attendance.recorded` | session ids, count | simulator advance |
| `forecast.updated` | course ids | weekly forecast close |
| `alert.created` / `alert.updated` | alert id, course, risk, status | weekly close, planner, approve, reject, dismiss, supersede |
| `planner_run.progress` | run id, alert id, samples done, n | planner, after every sample |
| `planner_run.completed` | run id, alert id, valid count | planner end (also infeasible and failed runs) |
| `plan.decided` | plan id, alert id, decision | approve / reject / dismiss |
| `sessions.changed` | session ids | approve, simulator, session and enrolment edits |
| `master_data.changed` | entity | drivers, trainers, courses CRUD |
| `calendar.sync_updated` | session id, status | sync worker |
| `sim.autoplay_changed` / `sim.autoplay_paused` | state / reason | auto-play |
| `heartbeat` | none | every 15 s |
| `resync` | reason | overflow, stale id, database re-seeded |

### Browser
`useLiveEvents` (mounted once in the app layout) opens one `EventSource` and maps events to react-query invalidations (`src/lib/liveMap.ts`),
batched for 300 ms. `planner_run.progress` is written straight into the cache. After a reconnect everything is invalidated once. If the stream is
down for more than 10 s the pill shows **Offline: polling** and active queries refresh every 15 s until it is back. The fixed-interval polling of
earlier phases was removed.

### Auto-play
`POST /api/sim/autoplay {"enabled": true, "step": "day"|"week", "interval_seconds": ≥3}` advances the clock on a timer. It pauses itself (and
publishes `sim.autoplay_paused`) when a planner run is active, at the end of the year, or if an advance fails.

### One process only
The bus, the sync worker and auto-play live inside one process. Run uvicorn with **one worker**. Several workers would need Postgres
LISTEN/NOTIFY (out of scope until Phase 9).
