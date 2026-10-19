# Training Optimiser

A mini ERP for a driver training department. It tracks annual training targets against actual completions per course, detects shortfall risk, and uses an agentic LLM planner to propose schedule changes. Every proposed plan is checked by a deterministic constraint checker, ranked, explained to the trainer in plain language, and (once approved) synced to Google Calendar. A responsible-AI layer covers hallucination checks, fairness of reallocation burden, and a full audit log.

Full brief: [docs/PROJECT_BRIEF.md](docs/PROJECT_BRIEF.md)

## How the AI part works

1. The forecast flags a course that is likely to miss its yearly target and opens an **alert**.
2. The **agent planner** (an LLM) is asked for N candidate plans. It can only look at data through **read-only tools** (upcoming sessions, rosters, eligible drivers, free trainer slots). It never writes to the database.
3. Each plan comes back in a strict JSON format and is re-checked **on the server** by a plain-Python **constraint checker** (capacity, availability, shifts, rest, trainer load, double booking, unknown IDs).
4. Every plan is stored, valid or not, with a full trace. An independent check counts invented IDs, so we can measure how often the model hallucinates and how often the checker catches it.

Details: [docs/agent_planner.md](docs/agent_planner.md) and [docs/constraint_rules.md](docs/constraint_rules.md).

## Stack

- **Backend:** FastAPI (Python 3.12), SQLAlchemy 2.0, pydantic-settings
- **Frontend:** React + Vite + TypeScript, Tailwind CSS, React Router
- **Database:** SQLite in dev, Postgres when deployed
- **LLM:** any OpenAI-compatible chat API with tool calling, behind one interface in `backend/app/llm/`. Groq by default; a local Ollama model also works (see below)
- **Calendar:** Google Calendar API (sandbox calendar), planned for Phase 7

## Running it (Windows, PowerShell)

### Backend

First time only:

```powershell
cd backend
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

Every time:

```powershell
cd backend
.venv\Scripts\Activate.ps1
uvicorn app.main:app --reload
```

The API runs on http://localhost:8000 and the interactive docs are at http://localhost:8000/docs.
Check http://localhost:8000/api/health: it should return `{"status":"ok","database":"ok","tables":13}`.

### Synthetic data and simulator (Phase 1)

Run from `backend/` with the virtual environment active. The app needs seeded data before the Simulation page shows anything.

```powershell
python -m app.simulator.seed --seed 42 --reset   # drop and regenerate the 2026 year (same seed = same data)
python -m app.simulator.report                   # writes docs/synthetic_data_report.md
pytest -q                                        # 216 tests, all with an in-memory database and a fake LLM (no network)
```

The Simulation page (http://localhost:5173/simulation) moves the simulated clock forward; the same controls exist at `/api/sim/advance`, `/api/sim/state`, `/api/sim/progress` and `/api/sim/reset` (see `/docs`). The clock cannot be moved while a planner run is in progress.

### Forecasting and risk flags (Phase 3)

The Dashboard shows, per course, actual completions against the target pace, a year-end forecast with a 90% range, P(hit target), a risk level and the reasons. The forecast uses the sessions still on the calendar ("pipeline forecast"), not a blind trend line. After every simulated Saturday the engine stores a forecast snapshot and opens, updates or auto-resolves alerts (each change is audited).

```powershell
python -m app.services.forecast_backtest --seeds 42 1 2 3 4   # writes docs/forecast_backtest.md (about 2 minutes)
```

API: `/api/tracking/summary`, `/api/tracking/courses`, `/api/tracking/courses/{id}`, `/api/tracking/series`, `POST /api/tracking/recompute`, `/api/alerts`.

### Constraint checker (Phase 4)

`POST /api/constraints/check` is a dry run: send a plan, get back every rule it breaks (24 violation codes in 8 categories) and nothing is written. Rules are listed in [docs/constraint_rules.md](docs/constraint_rules.md).

### Agent planner (Phase 5)

1. Put an LLM key in `backend/.env` (it is git-ignored; never commit it):

   ```
   GROQ_API_KEY=your_key
   GROQ_MODEL=openai/gpt-oss-120b
   ```

   The model must support tool calling. Without a key the planner returns a clear 503 and everything else still works.

   **Free tiers are small.** Groq's free tier allows about 8,000 tokens per minute and 200,000 per day for that model. The planner keeps prompts short and retries rate limits, but a full run can still hit the daily cap. A local model has no limits: install [Ollama](https://ollama.com), run `ollama pull qwen2.5:7b`, then set `GROQ_API_KEY=ollama`, `GROQ_BASE_URL=http://localhost:11434/v1`, `GROQ_MODEL=qwen2.5:7b` and `LLM_TIMEOUT_SECONDS=600`.

2. Seed, advance about 20 weeks on the Simulation page, open a course that has an open alert (Dashboard, then the course), and press **Generate plans**. You see progress, then each plan with its outcome (valid, invalid, schema invalid, no plan), the rules it broke, and the model's rationale. Plans are ranked and approved on the Alerts page (Phase 6).

3. Live smoke test against the real model (uses its own `smoke.db`, never `training.db`, and writes [docs/planner_report.md](docs/planner_report.md)):

   ```powershell
   python -m scripts.planner_smoke --n 3 --alerts 1
   ```

API: `POST /api/planner/runs`, `GET /api/planner/runs`, `GET /api/planner/runs/{id}`, `GET /api/planner/metrics`.

**Latest live result** (local `qwen2.5:7b`, 3 samples for one capacity-gap alert): 2 valid, 1 invalid (a driver who was unavailable, caught by the checker), 0 invented IDs. See the report for the example plan and cost per sample.

### Planner v2: why plans were invalid, and what changed

The first live runs produced only invalid plans: every sample booked night-shift drivers into a 09:00 session they could not attend, even though valid plans existed. The causes were tooling, not the model: no tool could check drivers for a *new* session time, the prompt showed only 09:00 slots, and the tool text promised two `validate_plan` calls while the code allowed one. What changed (details in [docs/agent_planner.md](docs/agent_planner.md)):

- **Time-aware tools:** `find_eligible_drivers` checks one session time (existing or new) and says why drivers were excluded; `suggest_session_times` offers windows where a trainer is free and drivers can attend; the alert context lists the pool by shift and which start hours work for each.
- **Repair and prune:** violations come back with a fix hint (3 `validate_plan` calls). If the final plan is still invalid, the rejected parts are removed and the result is kept as a separate `llm_pruned` plan. The raw LLM plan is stored untouched, so hallucination metrics stay honest.
- **Diversity:** distinct strategies, later samples are told what earlier ones did, near copies are marked duplicates.
- **Rule-based baseline:** every run also stores one plan made by plain code, so a valid option exists whenever one can.
- **Honest "no plan":** if no valid plan can exist, the AI is not called and the alert says why (for example no trainer free).
- **Lead time:** new sessions must start at least 48 hours from now. This is a planner policy; the constraint checker is unchanged.

Debug a run with `python -m scripts.debug_planner_run <run id>`; ask whether a valid plan exists with `python -m scripts.feasibility_check <alert id>`; compare before and after with `python -m scripts.planner_eval` (results in [docs/planner_report.md](docs/planner_report.md)). New settings: `PLANNER_MAX_VALIDATE_CALLS`, `PLANNER_MIN_LEAD_HOURS`, `PLANNER_MIN_NEW_SESSION_SIZE`, `PLANNER_GREEDY_BASELINE`, `PLANNER_DUPLICATE_OVERLAP`.

### Ranking and the alerts inbox (Phase 6)

When a planner run finishes, the valid plans are scored and ranked by plain code (closes the gap, chance of hitting the target, fairness, disruption, trainer time), the LLM writes a short message that is checked against the real numbers and names, and the alert becomes **Plans ready**. Open **Alerts**, choose "Act as trainer" at the top, and **Approve** or **Reject** a plan. Approving re-checks the plan against today's data, applies it in one transaction and queues the changes for the calendar (Phase 7). **The database schema changed: delete `backend/training.db` and re-seed.** Details: [docs/ranking_and_alerts.md](docs/ranking_and_alerts.md).

Live smoke test (own `smoke.db`, writes [docs/alerts_report.md](docs/alerts_report.md)):

```powershell
python -m scripts.smoke_phase6 --n 3 --alerts 3
```

### Frontend

First time only:

```powershell
cd frontend
npm install
Copy-Item .env.example .env
```

Every time:

```powershell
cd frontend
npm run dev
```

Open http://localhost:5173. A small dot in the top bar is green when the backend is running and red when it is not. The yellow clock in the top bar is the simulated date; "Next day" and "Next week" move it forward.

```powershell
npm test          # unit tests for the wording and number formatting (src/lib/format.ts)
npm run build     # type-check and production build
```

The design rules (colours, fonts, components, plain-language wording) are in [docs/design_system.md](docs/design_system.md).

### After pulling a schema change

Phases 3, 5 and 6 and planner v2 changed the database schema. If you have an older `backend/training.db`, delete it and re-seed:

```powershell
cd backend
Remove-Item training.db
python -m app.simulator.seed --seed 42 --reset
```

## Structure

- `backend/` : API, simulator, constraint checker, agent planner, services, tests
  - `app/llm/` : LLM client (Groq or any OpenAI-compatible API), fake client for tests, prompts, read-only agent tools, planner loop
  - `scripts/planner_smoke.py`, `smoke_phase6.py` : live planner and alerts tests with reports
  - `scripts/debug_planner_run.py`, `feasibility_check.py`, `planner_eval.py` : inspect a run, check whether a valid plan exists, compare before and after
- `frontend/` : React app
- `docs/` : project brief, constraint rules, planner notes and reports

## Build phases

| # | Phase | Status |
|---|---|---|
| 0 | Foundation: repo, FastAPI + React skeletons, DB schema | Done |
| 1 | Synthetic data + simulator | Done |
| 2 | ERP core: CRUD pages + calendar view | Done |
| 3 | Tracking + forecasting: dashboard charts, shortfall risk flags | Done |
| 4 | Constraint checker (built before the agent) | Done |
| 5 | Agent planner: LLM produces N structured plans from real DB lookups | Done |
| 6 | Ranking + alerts inbox: scored plans, LLM alerts, Approve / Reject | Done |
| 7 | Calendar sync + live updates | |
| 8 | Responsible AI: audit log, fairness panel, hallucination catch rate | |
| 9 | Eval, polish, deploy | |

## Known gaps

- The checker does not yet enforce the ERP's session-slot rule (Sunday to Thursday, 09:00 / 14:00 / 18:00 starts) for new sessions the agent adds. The tools and prompt steer the model to valid slots, but the rule should move into the checker before approved plans create sessions (Phase 6/7).
