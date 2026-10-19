"""Alerts inbox API: list, detail, generate / regenerate plans, reject, dismiss. Approving a plan lives in routers/plans.py."""
from collections.abc import Callable
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.llm import get_llm_client
from app.llm.base import LLMClient
from app.models import Alert, AlertDecision, AlertMessage, Course, Plan, PlannerRun, Trainer
from app.routers.planner import get_session_factory, queue_run, run_to_dict
from app.services import decisions
from app.services.errors import NotFound
from app.services.plan_view import describe_actions

router = APIRouter(prefix="/alerts", tags=["alerts"])

RISK_RANK = {"high": 2, "medium": 1, "low": 0}


class GenerateRequest(BaseModel):
    n: int | None = None


class RejectRequest(BaseModel):
    actor_trainer_id: int | None = None
    reason_code: str | None = None
    reason_text: str | None = None


class DismissRequest(BaseModel):
    actor_trainer_id: int | None = None
    reason_text: str | None = None


def _alert_dict(a: Alert, code: str, name: str) -> dict:
    return {
        "id": a.id, "course_id": a.course_id, "course_code": code, "course_name": name, "created_at": a.created_at,
        "updated_at": a.updated_at, "risk_level": a.risk_level, "shortfall_type": a.shortfall_type,
        "projected_completions": a.projected_completions, "target_completions": a.target_completions,
        "gap": max(0, a.target_completions - a.projected_completions),
        "message": a.message, "status": a.status, "details": a.details or {},
    }


@router.get("")
def list_alerts(
    status: str | None = None,
    risk_level: str | None = None,
    course_id: int | None = None,
    since: datetime | None = None,
    db: Session = Depends(get_db),
):
    """Alerts sorted by risk (high first), then newest simulated date. ``status`` may be a comma list.

    ``since`` keeps only alerts opened or changed at or after that sim time.
    """
    stmt = select(Alert, Course.code, Course.name).join(Course, Course.id == Alert.course_id)
    if status:
        stmt = stmt.where(Alert.status.in_([s.strip() for s in status.split(",")]))
    if risk_level:
        stmt = stmt.where(Alert.risk_level == risk_level)
    if course_id is not None:
        stmt = stmt.where(Alert.course_id == course_id)
    if since is not None:
        stmt = stmt.where(Alert.updated_at >= since)
    rows = list(db.execute(stmt.order_by(Alert.id.desc())))
    rows.sort(key=lambda r: (-RISK_RANK.get(r[0].risk_level, 0), -(r[0].updated_at or r[0].created_at).timestamp()))
    return [_alert_dict(a, code, name) for a, code, name in rows]


@router.get("/counts")
def alert_counts(db: Session = Depends(get_db)):
    """Alerts per status (for the sidebar badge, which shows ``plans_ready``)."""
    by_status = {s: n for s, n in db.execute(select(Alert.status, func.count()).group_by(Alert.status))}
    return {"by_status": by_status, "plans_ready": by_status.get("plans_ready", 0), "total": sum(by_status.values())}


def _plan_dict(db: Session, plan: Plan, texts: dict[int, dict], with_changes: bool = True) -> dict:
    text = texts.get(plan.id, {})
    out = {
        "id": plan.id, "run_id": plan.run_id, "rank": plan.rank, "status": plan.status, "outcome": plan.outcome,
        "source": plan.source, "parent_plan_id": plan.parent_plan_id,
        "score_total": plan.score_total, "score_breakdown": plan.score_breakdown or {},
        "fairness_flag": bool((plan.score_breakdown or {}).get("fairness_flag")),
        "rationale": text.get("rationale"), "tradeoffs": text.get("tradeoffs"), "agent_rationale": plan.rationale,
        "violations": plan.rejection_reasons or [], "fabricated_ids": plan.fabricated_ids or [],
        "is_duplicate": plan.is_duplicate,
    }
    if with_changes:
        out["changes"] = describe_actions(db, plan.actions)
    return out


def _message_dict(m: AlertMessage | None) -> dict | None:
    if m is None:
        return None
    return {
        "id": m.id, "model": m.model, "prompt_version": m.prompt_version, "message": m.parsed or {},
        "grounding_ok": m.grounding_ok, "grounding_issues": m.grounding_issues or [], "used_fallback": m.used_fallback,
        "attempts": m.attempts, "latency_ms": m.latency_ms, "created_at": m.created_at,
    }


def _approved_calendar(db: Session, alert_id: int) -> dict | None:
    """For an approved plan: the sessions it touched and their Google Calendar sync status."""
    from app.calendar.ops import plan_sessions

    plan_id = db.scalar(select(Plan.id).where(Plan.alert_id == alert_id, Plan.status == "approved").limit(1))
    return None if plan_id is None else {"plan_id": plan_id, "sessions": plan_sessions(db, plan_id)}


@router.get("/{alert_id}")
def get_alert(alert_id: int, db: Session = Depends(get_db)):
    """The alert, its forecast numbers, the latest message, ranked plans, rejected-by-checker plans and decision history."""
    alert = db.get(Alert, alert_id)
    if alert is None:
        raise NotFound(f"Alert {alert_id} not found")
    course = db.get(Course, alert.course_id)
    details = alert.details or {}
    message = db.scalar(select(AlertMessage).where(AlertMessage.alert_id == alert.id).order_by(AlertMessage.id.desc()).limit(1))
    texts = {p["plan_id"]: p for p in ((message.parsed or {}).get("plans", []) if message else [])}

    latest_run_id = db.scalar(select(func.max(Plan.run_id)).where(Plan.alert_id == alert.id))
    plans = list(db.scalars(select(Plan).where(Plan.alert_id == alert.id, Plan.run_id == latest_run_id).order_by(Plan.sample_index))) if latest_run_id else []
    ranked = sorted((p for p in plans if p.rank is not None), key=lambda p: p.rank)
    invalid = [p for p in plans if p.rank is None and p.status == "invalid"]

    run = db.scalar(select(PlannerRun).where(PlannerRun.alert_id == alert.id).order_by(PlannerRun.id.desc()).limit(1))
    trainers = {t.id: t.name for t in db.scalars(select(Trainer))}
    history = [
        {"id": d.id, "decision": d.decision, "plan_id": d.plan_id, "actor_trainer_id": d.actor_trainer_id,
         "actor_name": trainers.get(d.actor_trainer_id), "reason_code": d.reason_code, "reason_text": d.reason_text,
         "sim_time": d.sim_time}
        for d in db.scalars(select(AlertDecision).where(AlertDecision.alert_id == alert.id).order_by(AlertDecision.id))
    ]
    return {
        "alert": _alert_dict(alert, course.code, course.name),
        "forecast": {
            "target": alert.target_completions, "projected": details.get("projected", alert.projected_completions),
            "attended": details.get("attended"), "low": details.get("low"), "high": details.get("high"),
            "p_hit": details.get("p_hit"), "reasons": details.get("reasons", []),
        },
        "message": _message_dict(message),
        "top_k": settings.top_k,
        "presented": [_plan_dict(db, p, texts) for p in ranked if p.rank <= settings.top_k],
        "other_valid": [_plan_dict(db, p, texts) for p in ranked if p.rank > settings.top_k],
        "invalid": [_plan_dict(db, p, texts, with_changes=False) for p in invalid],
        "run": run_to_dict(db, run) if run else None,
        "decisions": history,
        "calendar": _approved_calendar(db, alert.id),
    }


@router.post("/{alert_id}/generate", status_code=202)
def generate(
    alert_id: int, body: GenerateRequest, background: BackgroundTasks, db: Session = Depends(get_db),
    llm: LLMClient = Depends(get_llm_client), session_factory: Callable[[], Session] = Depends(get_session_factory),
):
    """Start the planner for this alert (runs in the background; poll the alert or /planner/runs)."""
    alert = db.get(Alert, alert_id)
    if alert is None:
        raise NotFound(f"Alert {alert_id} not found")
    return queue_run(db, alert, body.n, llm, background, session_factory)


@router.post("/{alert_id}/regenerate", status_code=202)
def regenerate(
    alert_id: int, body: GenerateRequest, background: BackgroundTasks, db: Session = Depends(get_db),
    llm: LLMClient = Depends(get_llm_client), session_factory: Callable[[], Session] = Depends(get_session_factory),
):
    """Same as generate; when the new run finishes, the older plans of this alert become ``superseded``."""
    return generate(alert_id, body, background, db, llm, session_factory)


@router.post("/{alert_id}/reject")
def reject(alert_id: int, body: RejectRequest, db: Session = Depends(get_db)):
    return decisions.reject_alert(db, alert_id, body.actor_trainer_id, body.reason_code, body.reason_text)


@router.post("/{alert_id}/dismiss")
def dismiss(alert_id: int, body: DismissRequest, db: Session = Depends(get_db)):
    return decisions.dismiss_alert(db, alert_id, body.actor_trainer_id, body.reason_text)
