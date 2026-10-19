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
- **Agent:** `alerts` (course, sim created_at, risk_level, projected vs target, LLM message, status open/resolved/dismissed, plus Phase 3: `shortfall_type` capacity_gap/attendance_gap/pool_gap, `details` JSON {p_hit, low, high, reasons[], last_change, last_change_at, ...}, `updated_at` sim time), `alert_messages` / `alert_decisions` / `calendar_outbox` (Phase 6: message attempts with grounding results, trainer decisions, changes waiting for Phase 7 calendar sync), `plans` (alert, actions JSON, is_valid, rejection_reasons JSON, score, rank, status invalid/valid/approved/rejected/superseded/stale/expired, Phase 6 score_total / score_breakdown / scoring_version; Phase 5 adds run_id, sample_index, temperature, strategy, outcome, rationale, plan_hash, is_duplicate, draft_violations, fabricated_ids, checker_caught, trace, step/token/latency counts), `planner_runs` (one per Generate-plans click, with outcome counters), `audit_log` (actor agent/user/system, action, entity_type, entity_id, details JSON), `sim_state` (single row, current simulated time)
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

**Current status:** Phase 6 done plus planner v2 (fix for plans that were all invalid: `services/feasibility.py` is the single source for which drivers, trainers and times work, built on the checker's own rule functions; tools are time-aware: `find_eligible_drivers(course_id, session_id | start, end?)` returns `excluded_counts` and a by-shift `pool_summary` (never nationality), new `suggest_session_times`, `get_alert_context` adds pool by shift and start hours that work per shift; prompt `p6-v2`; `validate_plan` allows `PLANNER_MAX_VALIDATE_CALLS` (3) calls and returns `{code, driver_id, session_ref, fix_hint}` using `llm/fix_hints.py`; `llm/repair.prune_plan` makes a separate `llm_pruned` plan by removing rejected actions; near-duplicate samples (>80% overlap) are marked duplicates; `services/greedy_planner.py` stores a rule-based `greedy_baseline` plan in every run (`PLANNER_GREEDY_BASELINE`); if `assess()` finds no valid plan can exist the run makes zero LLM calls and stores `planner_runs.infeasibility`; planner-side lead time `PLANNER_MIN_LEAD_HOURS` (48, policy code `PLANNER_LEAD_TIME`, not a checker rule); every plan has `source` (llm / llm_pruned / greedy_baseline) and `parent_plan_id`, and run counters, `samples_done` and `/api/planner/metrics` count raw `llm` plans only; scripts `debug_planner_run`, `feasibility_check`, `planner_eval`; schema changed again, so `training.db` must be deleted and re-seeded; 275 backend tests passing; before/after numbers in `docs/planner_report.md`). Previous: Phase 6 done (ranking + alerts inbox: deterministic `services/ranking.py` scorer, five weighted components with a what-if forecast via `forecasting.project_moments`, fairness flag, `TOP_K` presented plans; `services/alert_writer.py` writes the trainer message from a fact pack with a deterministic grounding check, one retry and a template fallback, every attempt stored in `alert_messages`; `services/decisions.py` approve (re-check, expiry, one transaction, `calendar_outbox` rows, idempotent) / reject / dismiss with `alert_decisions`; `services/alert_pipeline.py` runs ranking and the writer at the end of every planner run and owns the alert status lifecycle open -> generating -> plans_ready | no_valid_plans -> approved | rejected | dismissed; endpoints `/api/alerts` (list, `/counts`, `/{id}`, generate, regenerate, reject, dismiss) and `/api/plans/{id}/approve|reject`; config `SCORE_WEIGHT_*`, `DISRUPTION_CAP`, `TRAINER_HOURS_CAP`, `TOP_K`, `AUTO_PLAN_ON_HIGH_RISK`; new tables `alert_messages`, `alert_decisions`, `calendar_outbox` and new `plans` columns, so `training.db` must be deleted and re-seeded; Alerts inbox page with ranked plan cards, Approve / Reject, act-as-trainer; `docs/ranking_and_alerts.md`; `python -m scripts.smoke_phase6` writes `docs/alerts_report.md`; 257 backend tests passing, none touching the network). Known gap: approve applies `add_session` without the Sun-Thu 09/14/18 slot rule, like the checker. Previous: Phase 5 done, plus a frontend redesign (new design system in `docs/design_system.md`: tokens, Barlow Semi Condensed + Public Sans, shared `src/components/ui/`, plain-language copy in `src/lib/format.ts` with vitest tests, new shell with the yellow simulated-clock control, redesigned Dashboard, Course detail, Alerts inbox, Calendar, four Records pages, Simulation; Audit log and Fairness are honest empty states until their backend exists; `npm test` runs the formatter tests) (agent planner: provider-agnostic `app/llm/` with a Groq httpx client and a scripted `FakeLLMClient`; 5 read-only agent tools that reuse the checker's rules via `driver_can_book` / `trainer_can_run`; planner loop that samples N plans per open alert, force-sets course/alert ids, re-checks every plan on the server and stores all outcomes with traces; independent hallucination check and catch-rate metric; `planner_runs` table plus new `plans` columns; `/api/planner/*` endpoints, sim clock frozen during a run; Generate plans panel on the course page; `docs/agent_planner.md`; `python -m scripts.planner_smoke` writes `docs/planner_report.md` when a key is set (run live once on a local Ollama qwen2.5:7b: 3 samples, 2 valid, 1 invalid, 0 hallucinated ids; Groq/HF/OpenRouter free tiers were blocked by quotas); schema changed, so `training.db` must be deleted and re-seeded; 216 backend tests passing, none touching the network). Known gap: the checker does not enforce the ERP session-slot rule (Sun-Thu, 09/14/18) for add_session. Phase 4 done (constraint checker: strict plan schema with `parse_plan`, deterministic read-only `check_plan` with 24 violation codes in 8 categories, applied action by action so intra-plan conflicts are caught, `POST /api/constraints/check` dry run, `docs/constraint_rules.md`; no schema change; 161 backend tests passing, including a 100% hallucination catch-rate test). Also fixed: the RSR 'healthy control' was not healthy (finished 0-14% above target, so the forecast flagged it high in several seeds); RSR now has a 74-78% target and 145% planned seats, and forecast `INFLATION` was re-tuned to 6, so `training.db` must be deleted and re-seeded. Phase 3 done (tracking + forecasting: weekly series, pipeline forecast with 90% range and P(hit), risk flags with shortfall type and structured reasons, alerts opened/updated/auto-resolved with audit rows, weekly forecast snapshots, Dashboard + course detail pages, `docs/forecast_backtest.md`; schema changed, so `training.db` must be deleted and re-seeded; 83 backend tests passing). Phase 1 done (synthetic data, simulator, sim API + Simulation page, validation report, tests). Phase 2 done (ERP core: CRUD API for drivers, trainers, courses, sessions and enrollments; calendar feed; Master Data and Calendar pages; 36 backend tests passing).

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
