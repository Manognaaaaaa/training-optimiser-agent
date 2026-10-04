# CLAUDE.md: Training Optimiser

Context file for Claude Code. Read this fully before doing anything in this repo.

## What we're building

**Dynamic Training Efficiency & Smart Calendar Optimiser**: a live, working web app (a mini ERP for a driver training department), not a set of scripts.

It tracks annual training targets vs actual completions per course, detects shortfall risk, and uses an **agentic LLM planner** that samples N candidate redistribution plans, validates each one with a deterministic **hard-constraint checker**, ranks the valid ones, writes a natural-language alert for the trainer, and syncs approved changes to **Google Calendar**. A **responsible-AI layer** covers hallucination checks, fairness of reallocation burden, and a full audit log.

Full brief: `docs/PROJECT_BRIEF.md`.

## Stack

- **Backend:** FastAPI (Python 3.12), SQLAlchemy 2.0 (typed `Mapped[...]` style), pydantic-settings
- **Frontend:** React + Vite + TypeScript, Tailwind CSS, React Router
- **Database:** SQLite in dev (`backend/training.db`), Postgres when deployed. Code must work on both (no SQLite-only SQL)
- **LLM:** Groq API (`GROQ_API_KEY`, `GROQ_MODEL` in `backend/.env`). Put all LLM calls behind one interface in `backend/app/llm/` so the provider can be swapped. Wired in Phase 5
- **Calendar:** Google Calendar API (sandbox calendar)

## Repo layout

```
Training_genai/
â”œâ”€â”€ CLAUDE.md
â”œâ”€â”€ README.md
â”œâ”€â”€ docs/PROJECT_BRIEF.md
â”œâ”€â”€ backend/
â”‚   â”œâ”€â”€ .venv/                 (git-ignored)
â”‚   â”œâ”€â”€ requirements.txt
â”‚   â””â”€â”€ app/
â”‚       â”œâ”€â”€ main.py            FastAPI app, CORS, lifespan creates tables
â”‚       â”œâ”€â”€ config.py          Settings from .env
â”‚       â”œâ”€â”€ database.py        engine, SessionLocal, Base, get_db
â”‚       â”œâ”€â”€ models/            master.py, scheduling.py, agent.py
â”‚       â”œâ”€â”€ schemas/           Pydantic request/response models
â”‚       â”œâ”€â”€ routers/           one file per resource
â”‚       â”œâ”€â”€ services/          business logic (tracking, forecasting, constraints, ranking, fairness, audit)
â”‚       â”œâ”€â”€ simulator/         synthetic data generator + sim clock
â”‚       â””â”€â”€ llm/               provider-agnostic LLM client, prompts, agent planner
â””â”€â”€ frontend/
    â””â”€â”€ src/
        â”œâ”€â”€ api/               typed API client
        â”œâ”€â”€ pages/             one file per page
        â”œâ”€â”€ components/
        â””â”€â”€ layouts/
```

## Data model (already defined in backend/app/models)

- **Master:** `drivers` (employee_code, name, nationality, shift day/night/rotating, depot, hire_date, is_active), `trainers` (name, max_sessions_per_week), `courses` (code, name, duration_hours, default_capacity, is_mandatory), `training_targets` (course_id, year, target_completions; unique per course+year)
- **Scheduling:** `training_sessions` (course, trainer, start/end, location, capacity, status scheduled/completed/cancelled, source seed/manual/agent, gcal_event_id), `enrollments` (session, driver, status booked/attended/no_show/cancelled; unique per session+driver), `driver_unavailability` (driver, start/end, reason)
- **Agent:** `alerts` (course, sim created_at, risk_level, projected vs target, LLM message, status open/resolved/dismissed, plus Phase 3: `shortfall_type` capacity_gap/attendance_gap/pool_gap, `details` JSON {p_hit, low, high, reasons[], last_change, last_change_at, ...}, `updated_at` sim time), `plans` (alert, actions JSON, is_valid, rejection_reasons JSON, score, rank, status), `audit_log` (actor agent/user/system, action, entity_type, entity_id, details JSON), `sim_state` (single row, current simulated time)
- **Forecasting (Phase 3):** `forecast_snapshots` (course, as_of sim time, attended, projected, low, high, p_hit, naive_projection, linear_projection, risk_level, shortfall_type; unique per course+as_of). One row per course per simulated week, written by `services/tracking_jobs.weekly_close` after each Saturday.

"Actual" completions = count of enrollments with status `attended` for a course in the target year.

If a phase needs a schema change, change the model, explain why, and (until Alembic is added) note that `training.db` must be deleted and re-seeded.

## Non-negotiable rules for the AI parts

1. **The LLM never writes to the DB directly.** It proposes structured plans (validated with Pydantic). Only the constraint checker + an explicit approve action change data.
2. **The LLM may only reference IDs that exist.** Give it real data via tool calls or DB lookups. Every plan goes through the constraint checker, which rejects unknown driver/session/trainer IDs. Track the catch rate.
3. **The constraint checker is plain, deterministic Python**, with clear rejection reasons (capacity, driver availability, trainer weekly load, rest rules, double booking). It's built and tested before the agent.
4. **Everything the agent or a user decides goes to `audit_log`** with enough detail to explain why.
5. **Fairness:** measure how reallocation burden is spread across shift and nationality. Flag or penalise plans that concentrate it on one group.
6. **Use simulated time** (`sim_state.current_time`) everywhere "now" matters, never the real clock, so fast-forward works.
7. No secrets in code. API keys go in `backend/.env` (git-ignored). Keep an up-to-date `backend/.env.example`.

## Build phases

| # | Phase | Done when |
|---|---|---|
| 0 | Foundation | Backend runs, 12 tables created, React app runs with sidebar + placeholder pages, frontend shows backend health |
| 1 | Synthetic data + simulator | Seed script creates a realistic year (drivers, trainers, courses, targets, sessions, enrollments, unavailability) with a fixed random seed; sim clock endpoint can fast-forward and stream attendance |
| 2 | ERP core | CRUD API + pages for drivers, trainers, courses, sessions; calendar view coloured by fill rate |
| 3 | Tracking + forecasting | Cumulative actual vs target curves per course; simple time-series forecast; at-risk flags on dashboard |
| 4 | Constraint checker | Validator with rejection reasons + unit tests (pytest) covering every rule |
| 5 | Agent planner | LLM samples N structured plans using real DB lookups; each plan validated |
| 6 | Ranking + alerts inbox | Scored and ranked plans, LLM alert text, Approve / Reject in UI |
| 7 | Calendar sync + live updates | Approved plans pushed to Google Calendar; UI auto-refreshes |
| 8 | Responsible AI | Audit log page, fairness panel, hallucination catch-rate metric |
| 9 | Eval, polish, deploy | Metrics report, README, hosted link |

**Current status:** Phase 4 done (constraint checker: strict plan schema with `parse_plan`, deterministic read-only `check_plan` with 24 violation codes in 8 categories, applied action by action so intra-plan conflicts are caught, `POST /api/constraints/check` dry run, `docs/constraint_rules.md`; no schema change; 161 backend tests passing, including a 100% hallucination catch-rate test). Also fixed: the RSR 'healthy control' was not healthy (finished 0-14% above target, so the forecast flagged it high in several seeds); RSR now has a 74-78% target and 145% planned seats, and forecast `INFLATION` was re-tuned to 6, so `training.db` must be deleted and re-seeded. Phase 3 done (tracking + forecasting: weekly series, pipeline forecast with 90% range and P(hit), risk flags with shortfall type and structured reasons, alerts opened/updated/auto-resolved with audit rows, weekly forecast snapshots, Dashboard + course detail pages, `docs/forecast_backtest.md`; schema changed, so `training.db` must be deleted and re-seeded; 83 backend tests passing). Phase 1 done (synthetic data, simulator, sim API + Simulation page, validation report, tests). Phase 2 done (ERP core: CRUD API for drivers, trainers, courses, sessions and enrollments; calendar feed; Master Data and Calendar pages; 36 backend tests passing).

## How to work in this repo

- **Environment:** Windows, PowerShell, VS Code. Give PowerShell commands, not bash.
- **Backend:** `cd backend`, `.venv\Scripts\Activate.ps1`, `uvicorn app.main:app --reload` (port 8000, docs at /docs)
- **Frontend:** `cd frontend`, `npm run dev` (port 5173)
- **One phase at a time.** Don't start the next phase unless asked.
- Before coding a phase: give a short plan (files to add/change, and why). Then build.
- After building: actually run it (start the server, hit the endpoints, run tests or the build) and fix errors before saying it's done.
- Keep code readable for a student: short docstrings on anything non-obvious, no clever one-liners.
- After install steps, update `requirements.txt` with `pip freeze | Out-File -Encoding utf8 requirements.txt` (never plain `>`, it writes UTF-16) or `package.json`.
- End each phase by summarising what changed, how to run and check it, and a suggested commit message. Update the **Current status** line in this file.
- Never commit `.env`, `*.db`, `.venv/`, or `node_modules/`.
