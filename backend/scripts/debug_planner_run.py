"""Print what the planning agent did in one planner run, sample by sample. Read-only.

Run from the backend folder:   python -m scripts.debug_planner_run <planner_run_id>

For each sample: strategy and temperature, the tool calls in order (name, arguments, short result), whether
validate_plan was used and what it returned, the agent's own (draft) violations against the server's final ones.
Uses DATABASE_URL (default backend/training.db), so it shows your real runs.
"""
import json
import sys
from collections import Counter

from sqlalchemy import select

from app.database import SessionLocal
from app.models import Alert, Plan, PlannerRun

SHORT = 160


def short(value, limit: int = SHORT) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text if len(text) <= limit else text[:limit] + "..."


def summarise_result(result: str) -> str:
    """The trace stores each tool result as text; show the useful part (counts, ids) rather than all of it."""
    try:
        data = json.loads(result)
    except (ValueError, TypeError):
        return short(result)
    if not isinstance(data, dict):
        return short(result)
    if "error" in data:
        return "ERROR: " + short(data["error"])
    parts = []
    for key in ("total_eligible", "total_matching", "free_seats", "is_valid", "total_booked"):
        if key in data:
            parts.append(f"{key}={data[key]}")
    for key in ("drivers", "sessions", "slots", "violations"):
        if isinstance(data.get(key), list):
            parts.append(f"{key}: {len(data[key])} item(s)")
    return ", ".join(parts) or short(result)


def main() -> int:
    if len(sys.argv) != 2 or not sys.argv[1].isdigit():
        print("usage: python -m scripts.debug_planner_run <planner_run_id>")
        return 2
    run_id = int(sys.argv[1])
    with SessionLocal() as db:
        run = db.get(PlannerRun, run_id)
        if run is None:
            print(f"No planner run {run_id}.")
            return 1
        alert = db.get(Alert, run.alert_id)
        print(f"Run {run.id}: alert {alert.id} ({alert.shortfall_type}), model {run.model}, prompt {run.prompt_version}, "
              f"{run.n_requested} samples, status {run.status}")
        print(f"  valid {run.n_valid}, invalid {run.n_invalid}, schema_invalid {run.n_schema_invalid}, "
              f"no_plan {run.n_no_plan}, step_limit {run.n_step_limit}, duplicates {run.n_duplicates}")
        for plan in db.scalars(select(Plan).where(Plan.run_id == run_id).order_by(Plan.sample_index)):
            print(f"\n=== Sample {plan.sample_index}: outcome {plan.outcome}, strategy '{plan.strategy}', temperature {plan.temperature}")
            print(f"    steps {plan.n_steps}, validate calls {plan.n_validate_calls}, tokens {plan.total_tokens}, {plan.latency_ms} ms")
            for entry in plan.trace or []:
                if "tool" in entry:
                    print(f"    tool {entry['tool']}({short(entry.get('args', ''), 110)}) -> {summarise_result(entry.get('result', ''))}")
                elif "note" in entry:
                    print(f"    note: {short(entry['note'])}")
                elif entry.get("text"):
                    print(f"    model said: {short(entry['text'])}")
            used = [e for e in (plan.trace or []) if e.get("tool") == "validate_plan"]
            print(f"    validate_plan used: {'yes' if used else 'NO'} ({len(used)} call(s))")
            for draft in plan.draft_violations or []:
                print(f"      draft check {draft.get('call')}: valid={draft.get('is_valid')} codes={Counter(draft.get('codes', []))}")
            final = Counter(v.get("code") for v in plan.rejection_reasons or [])
            print(f"    final violations: {dict(final) or 'none'}")
            actions = plan.actions if isinstance(plan.actions, list) else []
            adds = [a for a in actions if a.get("type") == "add_session"]
            for a in adds:
                print(f"    new session {a.get('temp_ref')}: {a.get('start')} to {a.get('end')}, trainer {a.get('trainer_id')}, capacity {a.get('capacity')}")
            drivers = sorted({a.get("driver_id") for a in actions if a.get("driver_id") is not None})
            print(f"    drivers assigned ({len(drivers)}): {drivers}")
            if plan.rationale:
                print(f"    rationale: {short(plan.rationale, 220)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
