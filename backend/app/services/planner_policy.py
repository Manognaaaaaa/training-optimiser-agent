"""Planner policy: things the agent should not propose even though the constraint checker would allow them.

These are NOT checker rules (docs/constraint_rules.md is unchanged, and approving a plan re-checks only with the
checker). They keep the agent's output sensible: for example, nobody should create a session that starts in 9 hours.
Violations use the same ``Violation`` type, with category ``policy``.
"""
from datetime import datetime, timedelta

from app.config import settings
from app.schemas.plan import AddSession, Plan, Violation

LEAD_TIME_CODE = "PLANNER_LEAD_TIME"


def earliest_new_session_start(now: datetime) -> datetime:
    """The earliest moment a NEW session may start under the lead-time policy."""
    return now + timedelta(hours=settings.planner_min_lead_hours)


def lead_time_violations(plan: Plan, now: datetime) -> list[Violation]:
    """One violation per add_session that starts sooner than the minimum lead time."""
    earliest = earliest_new_session_start(now)
    found = []
    for index, action in enumerate(plan.actions):
        if isinstance(action, AddSession) and action.start < earliest:
            found.append(Violation(
                code=LEAD_TIME_CODE, category="policy", action_index=index, entity_type="session", entity_id=action.temp_ref,
                message=(f"New session {action.temp_ref} starts {action.start:%a %d %b %H:%M}, but new sessions must start at least "
                         f"{settings.planner_min_lead_hours}h from now (not before {earliest:%a %d %b %H:%M})."),
            ))
    return found
