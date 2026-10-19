"""Keeps the ``alerts`` table in step with the forecasts. Called once a week by ``tracking_jobs.weekly_close``.

Rules:
- at most one open alert per course;
- a course that becomes medium/high with no open alert gets one (``alert_opened``);
- an open alert whose risk level changed is updated (``alert_escalated`` / ``alert_deescalated``);
- an open alert whose course is back to low or achieved is resolved (``alert_auto_resolved``);
- "open" here means any active alert (open, generating, plans_ready, no_valid_plans): its numbers are refreshed in place;
- a dismissed or rejected alert is a user decision and is never touched, but if the course gets worse than it was
  when dismissed (medium → high) a new alert is opened;
- the LLM message stays empty here (Phase 6 writes it).

Each opened / escalated / de-escalated / resolved alert writes exactly one audit row (actor "system").
Weekly refreshes of the numbers inside an open alert whose risk level did not change are not audited:
the numbers are kept week by week in ``forecast_snapshots`` instead.
"""
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Alert, Plan
from app.services.alert_pipeline import ACTIVE_STATUSES, CLOSED_BY_USER
from app.services.audit import log_event
from app.services.forecasting import CourseForecast
from app.services.risk import AT_RISK

RANK = {"low": 0, "medium": 1, "high": 2}


@dataclass
class AlertChanges:
    opened: list[int] = field(default_factory=list)  # alert ids
    escalated: list[int] = field(default_factory=list)
    deescalated: list[int] = field(default_factory=list)
    resolved: list[int] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.opened) + len(self.escalated) + len(self.deescalated) + len(self.resolved)


def alert_details(
    fc: CourseForecast, last_change: str, changed_at: datetime, previous_risk: str | None = None,
) -> dict:
    """The JSON stored in ``alerts.details``: everything the Phase 5 agent needs to choose a fix."""
    return {
        "course_code": fc.code,
        "p_hit": round(fc.p_hit, 3),
        "low": round(fc.low, 1),
        "high": round(fc.high, 1),
        "projected": round(fc.projected, 1),
        "attended": fc.attended,
        "p_show": round(fc.p_show, 3),
        "p_fill": round(fc.p_fill, 3),
        "remaining_seats": fc.remaining_seats,
        "eligible_pool": fc.eligible_pool,
        "seats_needed": round(fc.seats_needed, 1),
        "shortfall_type": fc.shortfall_type,
        "reasons": fc.reasons,
        "last_change": last_change,  # opened / escalated / deescalated / auto_resolved
        "last_change_at": changed_at.isoformat(),
        "previous_risk": previous_risk,
    }


def _audit(db: Session, now: datetime, action: str, alert: Alert, fc: CourseForecast, extra: dict | None = None) -> None:
    log_event(db, now, "system", action, "alert", alert.id, {
        "course": fc.code, "risk_level": fc.risk_level, "projected": round(fc.projected), "target": fc.target,
        "p_hit": round(fc.p_hit, 3), "shortfall_type": fc.shortfall_type,
        "reasons": [r["code"] for r in fc.reasons], **(extra or {}),
    })


def sync_alerts(db: Session, forecasts: list[CourseForecast], now: datetime) -> AlertChanges:
    """Apply the rules above for every course. Does not commit."""
    changes = AlertChanges()
    by_course: dict[int, list[Alert]] = {}
    for alert in db.scalars(select(Alert).order_by(Alert.id)):
        by_course.setdefault(alert.course_id, []).append(alert)

    for fc in forecasts:
        history = by_course.get(fc.course_id, [])
        open_alert = next((a for a in reversed(history) if a.status in ACTIVE_STATUSES), None)
        latest = history[-1] if history else None

        if open_alert is None:
            if fc.risk_level not in AT_RISK:
                continue
            if latest is not None and latest.status in CLOSED_BY_USER and RANK[fc.risk_level] <= RANK[latest.risk_level]:
                continue  # the user already said "not now"; only a worse level re-opens it
            alert = Alert(
                course_id=fc.course_id, created_at=now, updated_at=now, risk_level=fc.risk_level,
                projected_completions=round(fc.projected), target_completions=fc.target, message=None, status="open",
                shortfall_type=fc.shortfall_type, details=alert_details(fc, "opened", now),
            )
            db.add(alert)
            db.flush()  # need the id for the audit row
            _audit(db, now, "alert_opened", alert, fc)
            changes.opened.append(alert.id)
            continue

        if fc.risk_level in ("low", "achieved"):
            open_alert.status = "resolved"
            open_alert.updated_at = now
            for plan in db.scalars(select(Plan).where(Plan.alert_id == open_alert.id, Plan.status == "valid")):
                plan.status = "expired"  # the course recovered by itself, so the plans are no longer needed
            open_alert.projected_completions = round(fc.projected)
            open_alert.details = alert_details(fc, "auto_resolved", now, open_alert.risk_level)
            _audit(db, now, "alert_auto_resolved", open_alert, fc, {"previous_risk": open_alert.risk_level})
            changes.resolved.append(open_alert.id)
        elif fc.risk_level in AT_RISK:
            previous = open_alert.risk_level
            moved = fc.risk_level != previous
            last_change = open_alert.details.get("last_change", "opened")
            changed_at = datetime.fromisoformat(open_alert.details.get("last_change_at", now.isoformat()))
            if moved:
                last_change = "escalated" if RANK[fc.risk_level] > RANK[previous] else "deescalated"
                changed_at = now
            open_alert.risk_level = fc.risk_level
            open_alert.projected_completions = round(fc.projected)
            open_alert.target_completions = fc.target
            open_alert.shortfall_type = fc.shortfall_type
            open_alert.updated_at = now
            open_alert.details = alert_details(
                fc, last_change, changed_at, previous if moved else open_alert.details.get("previous_risk"),
            )
            if moved:
                _audit(db, now, f"alert_{last_change}", open_alert, fc, {"previous_risk": previous})
                (changes.escalated if last_change == "escalated" else changes.deescalated).append(open_alert.id)
        # insufficient_data: leave an existing alert alone
    return changes
