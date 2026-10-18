"""Planner API: start a run of the agent for an open alert, poll it, list runs, read the hallucination metrics.

The run itself happens in a FastAPI background task with its own database session. The request
returns 202 straight away with the run in status ``queued``.
"""
from collections.abc import Callable
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal, get_db
from app.llm import get_llm_client
from app.llm.base import LLMClient
from app.llm.planner import active_run_exists, create_run, execute_run
from app.models import Alert, Driver, Plan, PlannerRun, Trainer, TrainingSession
from app.services.errors import Conflict, Invalid, NotFound

router = APIRouter(prefix="/planner", tags=["planner"])


class RunRequest(BaseModel):
    alert_id: int
    n: int | None = Field(default=None)


def get_session_factory() -> Callable[[], Session]:
    """Dependency so tests can point the background task at the test database."""
    return SessionLocal


# --- Serialising ------------------------------------------------------------------------------------
def run_to_dict(db: Session, run: PlannerRun) -> dict:
    samples_done = len(db.scalars(select(Plan.id).where(Plan.run_id == run.id)).all())
    return {
        "id": run.id, "alert_id": run.alert_id, "course_id": run.course_id, "status": run.status,
        "n_requested": run.n_requested, "samples_done": samples_done,
        "n_valid": run.n_valid, "n_invalid": run.n_invalid, "n_schema_invalid": run.n_schema_invalid,
        "n_no_plan": run.n_no_plan, "n_step_limit": run.n_step_limit, "n_duplicates": run.n_duplicates,
        "n_llm_errors": run.n_llm_errors, "model": run.model, "prompt_version": run.prompt_version,
        "started_at": run.started_at, "finished_at": run.finished_at, "duration_ms": run.duration_ms,
        "total_tokens": run.total_tokens, "error": run.error,
    }


class _Labels:
    """Looks up driver / session / trainer names once for a whole response, so labels are not stored in the plan JSON."""

    def __init__(self, db: Session, plans: list[Plan]):
        driver_ids, session_ids, trainer_ids = set(), set(), set()
        for plan in plans:
            for a in plan.actions if isinstance(plan.actions, list) else []:
                if not isinstance(a, dict):
                    continue
                for key, bucket in (("driver_id", driver_ids), ("session_id", session_ids), ("from_session_id", session_ids),
                                    ("to_session_id", session_ids), ("trainer_id", trainer_ids)):
                    if isinstance(a.get(key), int) and not isinstance(a.get(key), bool):
                        bucket.add(a[key])
        self.drivers = {d.id: d.name for d in db.scalars(select(Driver).where(Driver.id.in_(driver_ids)))} if driver_ids else {}
        self.sessions = {s.id: s.start_time for s in db.scalars(select(TrainingSession).where(TrainingSession.id.in_(session_ids)))} if session_ids else {}
        self.trainers = {t.id: t.name for t in db.scalars(select(Trainer).where(Trainer.id.in_(trainer_ids)))} if trainer_ids else {}
        self.new_sessions: dict[str, str] = {}

    def driver(self, driver_id) -> str:
        return self.drivers.get(driver_id, f"unknown driver {driver_id}")

    def session(self, ref) -> str:
        if isinstance(ref, str):
            return self.new_sessions.get(ref, f"new session {ref}")
        start = self.sessions.get(ref)
        return f"{start:%a %d %b %H:%M}" if start else f"unknown session {ref}"

    def action(self, a) -> str:
        if not isinstance(a, dict):
            return "Unreadable action"
        kind = a.get("type")
        if kind == "enroll_driver":
            return f"Enrol {self.driver(a.get('driver_id'))} into {self.session(a.get('session_id'))}"
        if kind == "move_driver":
            return (f"Move {self.driver(a.get('driver_id'))} from {self.session(a.get('from_session_id'))} "
                    f"to {self.session(a.get('to_session_id'))}")
        if kind == "add_session":
            try:
                start = datetime.fromisoformat(str(a.get("start")))
                when = f"{start:%a %d %b %H:%M}"
            except ValueError:
                when = str(a.get("start"))
            ref = str(a.get("temp_ref"))
            self.new_sessions[ref] = f"new session {ref} ({when})"
            trainer = self.trainers.get(a.get("trainer_id"), f"unknown trainer {a.get('trainer_id')}")
            return f"Add session {ref} on {when} with {trainer} (capacity {a.get('capacity')})"
        return f"Unknown action {kind}"


def plan_to_dict(plan: Plan, labels: _Labels, include_trace: bool = False) -> dict:
    actions = plan.actions if isinstance(plan.actions, list) else []
    out = {
        "id": plan.id, "run_id": plan.run_id, "alert_id": plan.alert_id, "sample_index": plan.sample_index,
        "strategy": plan.strategy, "temperature": plan.temperature, "outcome": plan.outcome, "is_valid": plan.is_valid,
        "is_duplicate": plan.is_duplicate, "rationale": plan.rationale, "actions": actions,
        "action_labels": [labels.action(a) for a in actions],  # in order, so temp refs of earlier add_session resolve
        "violations": plan.rejection_reasons or [], "draft_violations": plan.draft_violations or [],
        "fabricated_ids": plan.fabricated_ids or [], "checker_caught": plan.checker_caught,
        "n_steps": plan.n_steps, "n_validate_calls": plan.n_validate_calls, "total_tokens": plan.total_tokens,
        "latency_ms": plan.latency_ms, "plan_hash": plan.plan_hash, "status": plan.status, "created_at": plan.created_at,
    }
    if include_trace:
        out["trace"] = plan.trace or []
    return out


# --- Endpoints ---------------------------------------------------------------------------------------
def _run_in_background(run_id: int, llm: LLMClient, session_factory: Callable[[], Session]) -> None:
    """Runs after the response is sent. Uses its own session, never the request's."""
    with session_factory() as db:
        execute_run(db, run_id, llm)


@router.post("/runs", status_code=202)
def start_run(
    body: RunRequest,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    llm: LLMClient = Depends(get_llm_client),
    session_factory: Callable[[], Session] = Depends(get_session_factory),
):
    """Queue N planning samples for an open alert."""
    alert = db.get(Alert, body.alert_id)
    if alert is None:
        raise NotFound(f"Alert {body.alert_id} not found")
    if alert.status != "open":
        raise Conflict(f"Alert {alert.id} is {alert.status}; plans can only be generated for open alerts")
    n = body.n if body.n is not None else settings.planner_default_n
    if not 1 <= n <= settings.planner_max_n:
        raise Invalid(f"n must be between 1 and {settings.planner_max_n}", {"n": "out of range"})
    if active_run_exists(db):
        raise Conflict("A planner run is already in progress")
    run = create_run(db, alert, n, getattr(llm, "model", None) or settings.groq_model)
    background.add_task(_run_in_background, run.id, llm, session_factory)
    return run_to_dict(db, run)


@router.get("/runs")
def list_runs(alert_id: int | None = None, db: Session = Depends(get_db)):
    stmt = select(PlannerRun).order_by(PlannerRun.id.desc())
    if alert_id is not None:
        stmt = stmt.where(PlannerRun.alert_id == alert_id)
    return [run_to_dict(db, r) for r in db.scalars(stmt)]


@router.get("/runs/{run_id}")
def get_run(run_id: int, trace: bool = Query(False), db: Session = Depends(get_db)):
    run = db.get(PlannerRun, run_id)
    if run is None:
        raise NotFound(f"Planner run {run_id} not found")
    plans = list(db.scalars(select(Plan).where(Plan.run_id == run_id).order_by(Plan.sample_index)))
    labels = _Labels(db, plans)
    return {**run_to_dict(db, run), "plans": [plan_to_dict(p, labels, trace) for p in plans]}


def _pct(part: int, whole: int) -> float:
    return round(100 * part / whole, 1) if whole else 0.0


@router.get("/metrics")
def metrics(db: Session = Depends(get_db)):
    """Hallucination and cost numbers across every stored agent plan. Phase 8 will draw a page from this."""
    plans = list(db.scalars(select(Plan).where(Plan.run_id.is_not(None))))
    total = len(plans)
    fabricated_total = caught_total = with_hallucination = 0
    for p in plans:
        violations = p.rejection_reasons or []
        flagged = {(v.get("entity_type"), v.get("entity_id")) for v in violations if v.get("category") == "hallucination"}
        fabricated = p.fabricated_ids or []
        fabricated_total += len(fabricated)
        caught_total += sum(1 for f in fabricated if (f["type"], f["id"]) in flagged)
        if fabricated or flagged:
            with_hallucination += 1
    outcomes: dict[str, int] = {}
    for p in plans:
        outcomes[p.outcome or "unknown"] = outcomes.get(p.outcome or "unknown", 0) + 1

    def avg(attr: str) -> float:
        return round(sum(getattr(p, attr) or 0 for p in plans) / total, 1) if total else 0.0

    return {
        "plans_total": total, "outcomes": outcomes,
        "valid_pct": _pct(outcomes.get("valid", 0), total),
        "schema_invalid_pct": _pct(outcomes.get("schema_invalid", 0), total),
        "hallucination_pct": _pct(with_hallucination, total),
        "fabricated_ids_total": fabricated_total, "fabricated_ids_caught": caught_total,
        "checker_catch_rate": round(caught_total / fabricated_total, 4) if fabricated_total else None,
        "avg_steps": avg("n_steps"), "avg_tokens": avg("total_tokens"), "avg_latency_ms": avg("latency_ms"),
        "runs_total": len(db.scalars(select(PlannerRun.id)).all()),
    }
