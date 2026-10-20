# Live updates (Phase 7)

```
any change ──► DB commit ──► event bus ──► SSE stream ──► react-query
```

Google Calendar sync was built and then removed on request. Approving a plan still writes `calendar_outbox` rows (a record of
each change), but nothing reads them and nothing is sent to Google.

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
| `sim.autoplay_changed` / `sim.autoplay_paused` | state / reason | auto-play |
| `heartbeat` | none | every 15 s |
| `resync` | reason | overflow, stale id, database re-seeded |

## Browser
`useLiveEvents` (mounted once in the app layout) opens one `EventSource` and maps events to react-query invalidations (`src/lib/liveMap.ts`),
batched for 300 ms. `planner_run.progress` is written straight into the cache. After a reconnect everything is invalidated once. If the stream is
down for more than 10 s the pill shows **Offline: polling** and active queries refresh every 15 s until it is back. The fixed-interval polling of
earlier phases was removed.

## Auto-play
`POST /api/sim/autoplay {"enabled": true, "step": "day"|"week", "interval_seconds": ≥3}` advances the clock on a timer. It pauses itself (and
publishes `sim.autoplay_paused`) when a planner run is active, at the end of the year, or if an advance fails. Controls are on the Simulation page.

## One process only
The bus and auto-play live inside one process. Run uvicorn with **one worker**. Several workers would need Postgres LISTEN/NOTIFY (out of scope until Phase 9).
