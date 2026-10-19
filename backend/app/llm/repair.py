"""Helping a plan that has violations: compact feedback for the agent, and a deterministic "prune" step.

Prune never invents anything. It only REMOVES the actions the checker rejected (and any new session left too small), then
checks again. A plan that becomes valid this way is stored as a separate plan with source ``llm_pruned``; the raw LLM plan
stays stored untouched, because the hallucination metrics must be computed on the model's own output.
"""
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.config import settings
from app.llm.fix_hints import hint_for
from app.schemas.plan import AddSession, Plan, Violation, parse_plan
from app.services.constraints import check_plan
from app.services.planner_policy import lead_time_violations
from app.services.ranking import ScoringContext, score_plan

MAX_PRUNE_ROUNDS = 8
MIN_EXPECTED_GAIN = 1.0  # a pruned plan must still add at least one expected completion


def all_violations(db: Session, plan: Plan, now: datetime) -> list[Violation]:
    """The checker's violations plus planner-policy violations (lead time) for one parsed plan."""
    return check_plan(db, plan, now).violations + lead_time_violations(plan, now)


def _session_ref(plan: Plan, violation: Violation) -> Any:
    """The session an offending action points at (a real id or a temp ref), taken from the action itself."""
    if violation.action_index is not None and violation.action_index < len(plan.actions):
        action = plan.actions[violation.action_index]
        if isinstance(action, AddSession):
            return action.temp_ref
        return getattr(action, "to_session_id", None) or getattr(action, "session_id", None)
    return violation.entity_id if violation.entity_type == "session" else None


def compact_violations(plan: Plan, violations: list[Violation], limit: int = 10) -> list[dict]:
    """``{code, driver_id, session_ref, action_index, fix_hint}`` per violation: short enough for a small model to act on."""
    return [
        {
            "code": v.code, "action_index": v.action_index,
            "driver_id": v.entity_id if v.entity_type == "driver" else None,
            "session_ref": _session_ref(plan, v), "fix_hint": hint_for(v.code),
        }
        for v in violations[:limit]
    ]


def _refs_used(action: dict) -> set:
    if action.get("type") == "add_session":
        return {action.get("temp_ref")}
    return {action.get("session_id"), action.get("to_session_id")} - {None}


def _drop_sessions(actions: list[dict], refs: set) -> list[dict]:
    """Remove the add_session actions with these temp refs and every action that uses them."""
    return [a for a in actions if not (a.get("type") == "add_session" and a.get("temp_ref") in refs)
            and not (a.get("type") != "add_session" and refs & _refs_used(a))]


def _too_small_sessions(actions: list[dict]) -> set:
    counts: dict[str, int] = {a["temp_ref"]: 0 for a in actions if a.get("type") == "add_session"}
    for a in actions:
        for ref in _refs_used(a):
            if ref in counts and a.get("type") != "add_session":
                counts[ref] += 1
    return {ref for ref, n in counts.items() if n < settings.planner_min_new_session_size}


def prune_plan(db: Session, now: datetime, raw_plan: dict, ctx: ScoringContext) -> tuple[dict | None, dict]:
    """Try to turn an invalid plan into a valid one by removing offending actions.

    Returns ``(pruned plan dict or None, info)``. ``info`` says what was removed or why nothing could be kept.
    """
    actions = [a for a in raw_plan.get("actions", []) if isinstance(a, dict)]
    original = len(actions)
    for _ in range(MAX_PRUNE_ROUNDS):
        if not actions:
            return None, {"reason": "nothing left after removing the rejected actions", "removed": original}
        parsed = parse_plan({**raw_plan, "actions": actions})
        if isinstance(parsed, Violation):
            return None, {"reason": "the plan could not be read", "removed": original - len(actions)}
        violations = all_violations(db, parsed, now)
        if violations:
            if any(v.action_index is None for v in violations):
                return None, {"reason": "a problem with the whole plan cannot be fixed by removing actions", "removed": original - len(actions)}
            bad = {v.action_index for v in violations}
            dropped_refs = {a["temp_ref"] for i, a in enumerate(actions) if i in bad and a.get("type") == "add_session"}
            actions = [a for i, a in enumerate(actions) if i not in bad]
            actions = _drop_sessions(actions, dropped_refs)
            continue
        small = _too_small_sessions(actions)
        if small:  # valid, but a new session with almost nobody in it is not worth running
            actions = _drop_sessions(actions, small)
            continue
        if not actions:
            break
        gain = score_plan(ctx, raw_plan["course_id"], actions)["expected_gain"]
        if gain < MIN_EXPECTED_GAIN:
            return None, {"reason": f"the remaining plan adds only {gain:.1f} expected completions", "removed": original - len(actions)}
        return {**raw_plan, "actions": actions}, {"removed": original - len(actions), "kept": len(actions), "expected_gain": gain}
    return None, {"reason": "still invalid after several rounds of removing actions", "removed": original - len(actions)}
