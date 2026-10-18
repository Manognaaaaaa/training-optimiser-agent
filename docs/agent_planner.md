# Agent planner (Phase 5)

When a course has an open alert, the agent produces **N candidate redistribution plans**. Every plan is built from real data the agent looked up with read-only tools, comes out in the strict `Plan` schema, and is checked by the Phase 4 constraint checker **on the server**. Valid or not, every sample is stored together with a full trace.

Not in this phase: scoring and ranking, fairness penalties, LLM alert text, approve / reject (Phase 6), Google Calendar (Phase 7). The planner never changes sessions or enrollments.

## The rules that never bend

1. The LLM never writes to the database. Tools are read-only. Only the planner service writes, and only to `planner_runs`, `plans` and `audit_log`.
2. The server always runs `parse_plan` + `check_plan` itself. The agent's own `validate_plan` result is advice, never trusted.
3. Every sample is stored. An invalid plan is data for the hallucination metric.
4. "Now" is `get_sim_now`, read once at the start of a run. `POST /sim/advance` and `/sim/reset` return 409 while a run is `queued` or `running`.
5. Tests use `FakeLLMClient`; none touches the network.

## Files

| File | Role |
|---|---|
| `app/llm/base.py` | `LLMClient` protocol, `LLMResponse`, `ToolCall`, `LLMError(kind)` |
| `app/llm/groq_client.py` | `GroqClient` (httpx, OpenAI-compatible tool calling, 3 attempts on 429 / 5xx, honours `retry-after`) |
| `app/llm/fake.py` | `FakeLLMClient(script)` for tests |
| `app/llm/prompts.py` | `PROMPT_VERSION = "p5-v1"`, short system prompt (hand-written action formats, about 800 tokens), strategies, temperatures |
| `app/llm/tools.py` | the read-only tools and their JSON schemas |
| `app/llm/planner.py` | the agent loop, plan storage, dedupe, hallucination check, audit |
| `app/routers/planner.py` | `POST/GET /api/planner/runs`, `GET /api/planner/metrics` |

`get_llm_client()` is a FastAPI dependency: it raises `LLMError("unavailable")` (HTTP 503, "GROQ_API_KEY is not set in backend/.env") when no key is set. The model in `GROQ_MODEL` must support tool calling on Groq; if Groq rejects the request, the error says so.

## The loop (one sample)

1. Pick a **temperature** and a **strategy hint** (below). Build `[system, user]` messages.
2. Send messages + tool specs. Run each tool call (read-only tools through `run_read_only_tool`; `validate_plan`, `submit_plan`, `submit_no_plan` inside the planner). Append results. Repeat until `submit_plan` / `submit_no_plan` or `planner_max_steps` (default 8).
3. Malformed tool arguments: an error message goes back to the model and the loop continues (it costs a step). If the model answers in plain text instead of calling a tool, the first reply is searched for a JSON object once; later text replies get a nudge to call `submit_plan`.
4. The server overwrites `course_id` and `alert_id` with the alert's values (a note goes in the trace if the model used different ones), then `parse_plan` + `check_plan`.

### Outcomes

`valid`, `invalid` (checker violations), `schema_invalid` (does not parse), `no_plan` (`submit_no_plan`), `step_limit` (never submitted), and `llm_error` (the LLM failed after retries: the run is marked `failed`, plans already stored are kept, no row is written for that sample).

## Tools

All results are compact JSON, capped at `MAX_TOOL_ROWS = 25` rows (`"truncated": true` when cut). Bad arguments return `{"error": "..."}`.

| Tool | Returns | Checker rules reused |
|---|---|---|
| `get_alert_context(alert_id)` | course, target, attended, projected, low/high, p_hit, shortfall type, reasons, gap, eligible pool, sim now, booking window | none (reads the alert's stored details) |
| `list_upcoming_sessions(course_id, from_date?, to_date?, min_free_seats?)` | future scheduled sessions: booked, free seats, fill rate, `inside_booking_window` | none |
| `get_session_roster(session_id)` | booked drivers: id, name, shift, depot | none |
| `find_eligible_drivers(course_id, session_id, limit?, move_from_session_id?)` | drivers who can be booked into the session | every `BOOKING_RULES` rule, through `driver_can_book` |
| `find_trainer_slots(course_id, from_date?, to_date?, limit?)` | `(trainer_id, start, end)` slots for a new session | every `ADD_SESSION_RULES` rule, through `trainer_can_run` |
| `validate_plan(plan)` | `is_valid` + violations from the real checker; **max 1 call per sample** | all of `check_plan` |
| `submit_plan(plan)` | ends the sample | |
| `submit_no_plan(reason)` | ends the sample, counted as `no_plan` | |

`find_eligible_drivers` takes an optional `move_from_session_id`: it then lists drivers already booked in that session who could be moved into the target (the old seat is freed first, exactly as `check_plan` does for `move_driver`).

`find_trainer_slots` tries start times on a fixed grid: Sunday to Thursday at 09:00, 14:00 and 18:00 (`SESSION_START_HOURS` / `SESSION_WEEKDAYS` from `simulator/rules.py`, the same slots the ERP enforces) and keeps those with no violation.

The helpers `driver_can_book` and `trainer_can_run` in `services/constraints.py` call the existing rule lists, so a tool can never offer something the checker would refuse. No rule behaviour changed; `load_snapshot_for_ids` is `load_snapshot` split so the tools can ask about many candidates with one read.

## Token budget (Groq free tier)

Free-tier limits are small (for example 8,000 tokens per minute and 200,000 per day on `openai/gpt-oss-120b`), and every step re-sends the conversation. So the planner: looks up the alert context and the first upcoming sessions with free seats itself and puts them in the first prompt; keeps the system prompt short; defaults tool lists to 6 rows; reserves only 800 reply tokens; and drops the oldest tool results when a request passes `PLANNER_CONTEXT_CHAR_BUDGET` characters. A 429 with a long `retry-after` (a daily limit) fails fast with Groq's message.

## Strategy hints and temperatures

Samples are sequential (Groq rate limits). Temperatures cycle through `[0.2, 0.5, 0.7, 0.9, 1.0]`; the hint rotates through a list for the alert's `shortfall_type`:

- `capacity_gap`: add sessions and fill them / add fewer, larger sessions / mix new sessions with filling existing free seats
- `attendance_gap`: fill free seats in upcoming sessions with extra eligible drivers / move drivers away from sessions that clash with their shift into ones that fit / prioritise sessions inside the booking window
- `pool_gap`: enrol eligible drivers who have no booking yet / spread new bookings across several sessions

Both are stored on each plan.

## Dedupe

`plan_hash` = SHA-256 of the canonical JSON of the actions: keys sorted, `enroll_driver` / `move_driver` actions sorted, `add_session` actions kept in order (temp refs depend on it). A plan whose hash was already seen in the same run is stored with `is_duplicate = true`; the run counts it under `n_duplicates` instead of its outcome column, so the counters always add up to `n_requested`.

## Hallucination check vs the checker

The check is deliberately **separate** from the checker so we can measure how well the checker does on real LLM output:

1. `fabricated_ids`: collect every driver / session / trainer / course ID in the raw plan JSON and look each up with a plain query. Those that do not exist are fabricated.
2. `checker_caught`: true when every fabricated ID got a violation in the `hallucination` category (`UNKNOWN_DRIVER`, `UNKNOWN_SESSION`, ...) for the same entity. `null` when nothing was fabricated or the plan failed schema validation (the checker never ran).

`GET /api/planner/metrics` rolls these up: valid %, schema-invalid %, % of plans with any hallucination, fabricated IDs total, checker catch rate on fabricated IDs, average steps / tokens / latency per sample.

## Why nationality is hidden from the model

Shift matters to the rules (a night-shift driver cannot start before 12:00), so tools show it. Nationality is never returned by any tool. We do not want the model choosing people by nationality; fairness by shift and nationality is **measured in code** afterwards (Phase 6 scoring, Phase 8 panel), not left to the model. A test checks that no tool output or spec contains the word.

## Known gap

The checker (Phase 4) does not enforce the ERP's session-slot rule (Sunday to Thursday, 09:00 / 14:00 / 18:00 starts) for `add_session`; the slot tool and the prompt steer the model to valid slots, but an off-grid new session would pass `check_plan`. Phase 6/7 should add that rule to the checker before approved plans create sessions.

## Data

- `planner_runs`: alert, course, status (`queued` / `running` / `done` / `failed`), requested count, one counter per outcome, model, prompt version, sim start / finish, real `duration_ms`, total tokens, error.
- `plans` gains: `run_id`, `sample_index`, `temperature`, `strategy`, `outcome`, `rationale`, `plan_hash`, `is_duplicate`, `draft_violations` (the agent's own `validate_plan` results), `fabricated_ids`, `checker_caught`, `trace` (each assistant step, tool call, argument and a short result summary; the system prompt is not stored, only `prompt_version`), `n_steps`, `n_validate_calls`, `total_tokens`, `latency_ms`, `created_at`. Existing columns keep their meaning: `rejection_reasons` = final checker violations, `is_valid`, `status = proposed`, `score` / `rank` null until Phase 6.

Audit rows: `planner_run_started` / `planner_run_finished` / `planner_run_failed` (actor `system`), `plan_proposed` per stored plan (actor `agent`). Never the API key or full prompts.

## Running it

Set `GROQ_API_KEY` (and optionally `GROQ_MODEL`) in `backend/.env`, delete `backend/training.db`, re-seed, advance about 20 weeks on the Simulation page, open a course with an open alert and press **Generate plans**.

Live smoke test (own database `backend/smoke.db`, writes `docs/planner_report.md`):

```powershell
cd backend
.venv\Scripts\Activate.ps1
python -m scripts.planner_smoke            # 3 alerts, 5 samples each
python -m scripts.planner_smoke --n 3 --alerts 1
```

With no key set it prints a message and exits 0.
