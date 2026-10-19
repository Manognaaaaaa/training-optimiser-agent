"""What happens after a planner run finishes: rank the valid plans, write the alert text, update the alert status.

Alert status lifecycle (plans have their own, see docs/ranking_and_alerts.md):

    open -> generating -> plans_ready | no_valid_plans -> approved | rejected | dismissed
    (plus resolved / expired / superseded set by the weekly alert sync)

``ACTIVE_STATUSES`` are the alerts a course can have at most one of: the weekly sync updates them in place.
"""
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.llm.base import LLMClient
from app.models import Alert, Plan, PlannerRun
from app.services.alert_writer import write_alert_message
from app.services.audit import log_event
from app.services.ranking import build_context, rank_plans
from app.services.sim_clock import get_sim_now

ACTIVE_STATUSES = ("open", "generating", "plans_ready", "no_valid_plans")
CAN_GENERATE = ("open", "no_valid_plans", "plans_ready")  # states a planner run may start from
CLOSED_BY_USER = ("dismissed", "rejected")  # the weekly sync only reopens these when the risk gets worse


def set_alert_status(db: Session, alert: Alert, status: str, reason: str) -> None:
    """Change an alert's status and leave an audit row. Does not commit."""
    if alert.status == status:
        return
    previous, alert.status = alert.status, status
    log_event(db, get_sim_now(db), "system", "alert_status_changed", "alert", alert.id,
              {"from": previous, "to": status, "reason": reason})


def restore_after_failed_run(db: Session, alert: Alert, reason: str) -> None:
    """A run that did not finish leaves the alert as it was: plans_ready if good plans exist, else open."""
    if alert.status != "generating":
        return
    has_plans = db.scalar(select(Plan.id).where(Plan.alert_id == alert.id, Plan.status == "valid", Plan.rank.is_not(None)).limit(1))
    set_alert_status(db, alert, "plans_ready" if has_plans else "open", reason)


def finalize_run(db: Session, run_id: int, llm: LLMClient) -> None:
    """Rank the run's valid plans, write the message, set the alert status, commit. Older plans become superseded."""
    run = db.get(PlannerRun, run_id)
    alert = db.get(Alert, run.alert_id)
    now = get_sim_now(db)

    for old in db.scalars(select(Plan).where(Plan.alert_id == alert.id, Plan.run_id != run_id, Plan.status == "valid")):
        old.status = "superseded"

    ranked = rank_plans(db, run_id)
    presented = ranked[: settings.top_k]
    ctx = build_context(db, alert, now)
    write_alert_message(db, llm, alert, run, presented, ctx)
    set_alert_status(db, alert, "plans_ready" if ranked else "no_valid_plans", f"planner run {run_id} finished with {len(ranked)} ranked plan(s)")
    db.commit()
