"""The agent planner: asks the LLM for N candidate plans for one alert and stores every one of them.

Safety rules this module enforces (see docs/agent_planner.md):
- The LLM only gets read-only tools. This module is the only writer, and it writes only to
  ``planner_runs``, ``plans`` and ``audit_log``.
- The server always parses and checks the final plan itself; it never trusts the agent's "valid".
- Every sample is stored, valid or not. An invalid plan is data for the hallucination metric.
- "Now" is the simulated clock, read once at the start of the run.
"""
import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.llm import prompts
from app.llm.base import LLMClient, LLMError
from app.llm.tools import TOOL_SPECS, run_read_only_tool
from app.models import Alert, Course, Driver, Plan, PlannerRun, Trainer, TrainingSession
from app.schemas.plan import Violation, parse_plan
from app.services.audit import log_event
from app.services.constraints import CATEGORY, check_plan
from app.services.sim_clock import get_sim_now

MAX_VALIDATE_CALLS = 1
MAX_FORMAT_REPAIRS = 1  # a plan with a format mistake is sent back once, with the exact error, before it is rejected
TOOLS_CHARS = len(json.dumps(TOOL_SPECS))  # the tool schemas are sent with every request too
MAX_TOKENS_PER_STEP = 800  # Groq counts this reserve against the per-minute token limit
PREFETCH_SESSIONS = 5
PREFETCH_ELIGIBLE_SESSIONS = 2
PREFETCH_DRIVERS_PER_SESSION = 6
PREFETCH_SLOTS = 4
TRACE_TEXT_LIMIT = 500
RATIONALE_AUDIT_LIMIT = 500

OUTCOMES = ("valid", "invalid", "schema_invalid", "no_plan", "step_limit")
ACTIVE_STATUSES = ("queued", "running")


# --- Run bookkeeping ------------------------------------------------------------------------------
def active_run_exists(db: Session) -> bool:
    return db.scalar(select(PlannerRun.id).where(PlannerRun.status.in_(ACTIVE_STATUSES)).limit(1)) is not None


def mark_stale_runs_failed(db: Session) -> int:
    """At startup: a run left queued/running by a crash can never finish, so mark it failed."""
    stale = list(db.scalars(select(PlannerRun).where(PlannerRun.status.in_(ACTIVE_STATUSES))))
    for run in stale:
        run.status = "failed"
        run.error = "server restarted"
    db.commit()
    return len(stale)


def create_run(db: Session, alert: Alert, n: int, model: str) -> PlannerRun:
    run = PlannerRun(
        alert_id=alert.id, course_id=alert.course_id, status="queued", n_requested=n,
        model=model, prompt_version=prompts.PROMPT_VERSION,
    )
    db.add(run)
    db.commit()
    return run


# --- Small helpers --------------------------------------------------------------------------------
def _cut(text: Any, limit: int = TRACE_TEXT_LIMIT) -> str:
    text = text if isinstance(text, str) else json.dumps(text, default=str)
    return text if len(text) <= limit else text[:limit] + "..."


def _extract_json_object(text: str) -> dict | None:
    """Pull the first JSON object out of a plain-text reply (the model forgot to call submit_plan)."""
    start = text.find("{")
    if start == -1:
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(text[start:])
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def referenced_ids(raw_plan: dict) -> dict[str, set[int]]:
    """Every driver / session / trainer / course id a raw plan mentions (read straight from the JSON, best effort)."""
    found: dict[str, set[int]] = {"driver": set(), "session": set(), "trainer": set(), "course": set()}
    if _is_int(raw_plan.get("course_id")):
        found["course"].add(raw_plan["course_id"])
    actions = raw_plan.get("actions")
    for action in actions if isinstance(actions, list) else []:
        if not isinstance(action, dict):
            continue
        for key, kind in (
            ("driver_id", "driver"), ("session_id", "session"), ("from_session_id", "session"),
            ("to_session_id", "session"), ("trainer_id", "trainer"), ("course_id", "course"),
        ):
            if _is_int(action.get(key)):
                found[kind].add(action[key])
    return found


def find_fabricated_ids(db: Session, raw_plan: dict) -> list[dict]:
    """IDs the plan mentions that are not in the database. A plain lookup, independent of the checker."""
    ids = referenced_ids(raw_plan)
    models = {"driver": Driver, "session": TrainingSession, "trainer": Trainer, "course": Course}
    fabricated = []
    for kind, wanted in ids.items():
        if not wanted:
            continue
        model = models[kind]
        existing = set(db.scalars(select(model.id).where(model.id.in_(wanted))))
        fabricated.extend({"type": kind, "id": i} for i in sorted(wanted - existing))
    return fabricated


def checker_caught_all(fabricated: list[dict], violations: list[Violation]) -> bool | None:
    """Did the checker report a hallucination-category violation for every fabricated id? None when nothing was fabricated."""
    if not fabricated:
        return None
    flagged = {
        (v.entity_type, v.entity_id) for v in violations if CATEGORY.get(v.code) == "hallucination" or v.category == "hallucination"
    }
    return all((f["type"], f["id"]) in flagged for f in fabricated)


def compute_plan_hash(actions: Any) -> str | None:
    """SHA-256 of the canonical actions. add_session keeps its order (temp refs depend on it); other actions are sorted."""
    if not isinstance(actions, list) or not actions:
        return None
    adds = [a for a in actions if isinstance(a, dict) and a.get("type") == "add_session"]
    others = [a for a in actions if not (isinstance(a, dict) and a.get("type") == "add_session")]

    def sort_key(a: Any) -> tuple:
        a = a if isinstance(a, dict) else {}
        return (str(a.get("type")), str(a.get("driver_id")), str(a.get("session_id", a.get("to_session_id"))))

    canonical = {"adds": adds, "others": sorted(others, key=sort_key)}
    text = json.dumps(canonical, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode()).hexdigest()


def summarise_actions(actions: Any) -> str:
    if not isinstance(actions, list):
        return "no actions"
    counts: dict[str, int] = {}
    for a in actions:
        kind = a.get("type", "?") if isinstance(a, dict) else "?"
        counts[kind] = counts.get(kind, 0) + 1
    labels = {"enroll_driver": "enrol", "move_driver": "move", "add_session": "add_session"}
    return ", ".join(f"{n} {labels.get(k, k)}" for k, n in counts.items()) or "no actions"


# --- One sample -----------------------------------------------------------------------------------
@dataclass
class SampleState:
    messages: list[dict] = field(default_factory=list)
    trace: list[dict] = field(default_factory=list)
    draft_violations: list[dict] = field(default_factory=list)
    n_steps: int = 0
    n_validate: int = 0
    tokens: int = 0
    model: str = ""
    final_plan: dict | None = None  # raw plan the agent submitted
    no_plan_reason: str | None = None
    text_fallback_used: bool = False
    n_repairs: int = 0


def _force_ids(raw: dict, alert: Alert, state: SampleState | None = None) -> dict:
    """The plan must be about this alert's course and alert. Overwrite anything else and note it in the trace."""
    fixed = dict(raw)
    for key, wanted in (("course_id", alert.course_id), ("alert_id", alert.id)):
        if key in fixed and fixed[key] != wanted and state is not None:
            state.trace.append({"note": f"model sent {key}={fixed[key]!r}; server overwrote it with {wanted}"})
        fixed[key] = wanted
    return fixed


def _validate_for_agent(db: Session, now: datetime, alert: Alert, raw: Any, state: SampleState) -> dict:
    """What the validate_plan tool returns: the real checker's verdict."""
    if not isinstance(raw, dict):
        return {"error": "plan must be a JSON object"}
    parsed = parse_plan(_force_ids(raw, alert))
    if isinstance(parsed, Violation):
        violations = [parsed]
        result = {"is_valid": False, "violations": [{"code": parsed.code, "message": parsed.message}]}
    else:
        check = check_plan(db, parsed, now)
        violations = check.violations
        result = {
            "is_valid": check.is_valid,
            "violations": [
                {"code": v.code, "action_index": v.action_index, "message": v.message} for v in violations[:10]
            ],
            "truncated": len(violations) > 10,
        }
    state.draft_violations.append({"call": state.n_validate, "is_valid": result["is_valid"], "codes": [v.code for v in violations]})
    return result


OMITTED_NOTE = '{"note":"older result omitted to save space; call the tool again if you need it"}'


def prune_messages(messages: list[dict], budget_chars: int) -> int:
    """Keep the request small: replace the oldest tool results with a short note until the request fits the budget.

    Groq's free tier allows few tokens per minute and every step re-sends the whole conversation, so old tool
    output is the first thing to drop. The assistant's own messages and the newest tool results are kept.
    Returns how many tool results were replaced.
    """
    def size() -> int:
        return len(json.dumps(messages, default=str)) + TOOLS_CHARS

    replaced = 0
    for message in messages:
        if size() <= budget_chars:
            break
        if message.get("role") == "tool" and message.get("content") != OMITTED_NOTE and len(message.get("content", "")) > len(OMITTED_NOTE):
            message["content"] = OMITTED_NOTE
            replaced += 1
    return replaced


def _assistant_message(resp) -> dict:
    message: dict[str, Any] = {"role": "assistant", "content": resp.content}
    if resp.tool_calls:
        message["tool_calls"] = [
            {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}} for c in resp.tool_calls
        ]
    return message


def _prefetch_candidates(db: Session, now: datetime, alert: Alert, sessions: dict) -> dict:
    """Drivers who can be booked into the first sessions, and open trainer slots. All from the same tools the model could call."""
    out: dict[str, Any] = {}
    by_session: dict[str, list] = {}
    for session in (sessions.get("sessions") or [])[:PREFETCH_ELIGIBLE_SESSIONS]:
        found = run_read_only_tool(db, now, "find_eligible_drivers", {
            "course_id": alert.course_id, "session_id": session["session_id"], "limit": PREFETCH_DRIVERS_PER_SESSION})
        by_session[str(session["session_id"])] = [{"driver_id": d["driver_id"], "name": d["name"]} for d in found.get("drivers", [])]
    if by_session:
        out["eligible_drivers_by_session_id"] = by_session
    slots = run_read_only_tool(db, now, "find_trainer_slots", {"course_id": alert.course_id, "limit": PREFETCH_SLOTS})
    if slots.get("slots"):
        out["open_new_session_slots"] = slots["slots"]
    return out


def run_agent_loop(db: Session, now: datetime, alert: Alert, llm: LLMClient, temperature: float, strategy: str) -> SampleState:
    """Let the model call tools until it submits (or runs out of steps). Raises LLMError if the LLM fails."""
    state = SampleState()
    # Look up the basics ourselves, once, so the model does not spend steps (and tokens) asking for them.
    facts = {
        "alert": run_read_only_tool(db, now, "get_alert_context", {"alert_id": alert.id}),
        "sessions_with_free_seats": run_read_only_tool(
            db, now, "list_upcoming_sessions", {"course_id": alert.course_id, "min_free_seats": 1, "limit": PREFETCH_SESSIONS}),
    }
    facts.update(_prefetch_candidates(db, now, alert, facts["sessions_with_free_seats"]))
    facts_text = json.dumps(facts, separators=(",", ":"), default=str)
    state.trace.append({"note": "alert context, upcoming sessions, eligible drivers and open slots were looked up by the server and put in the prompt"})
    state.messages = [
        {"role": "system", "content": prompts.system_prompt()},
        {"role": "user", "content": prompts.sample_prompt(alert.id, alert.course_id, strategy, facts_text)},
    ]
    for _ in range(settings.planner_max_steps):
        pruned = prune_messages(state.messages, settings.planner_context_char_budget)
        if pruned:
            state.trace.append({"note": f"{pruned} old tool result(s) dropped from the prompt to stay under the size budget"})
        try:
            resp = llm.chat(state.messages, tools=TOOL_SPECS, temperature=temperature, max_tokens=MAX_TOKENS_PER_STEP)
        except LLMError as err:
            if err.kind != "bad_request" or "validation failed" not in err.message.lower():
                raise
            # The provider rejected the model's tool call (e.g. wrong argument type). That is the model's mistake,
            # not an outage: tell it and let it try again, like any other malformed tool call.
            state.n_steps += 1
            state.trace.append({"step": state.n_steps, "note": f"provider rejected the tool call: {_cut(err.message, 300)}"})
            state.messages.append({"role": "user", "content": "Your last tool call was rejected as invalid. Send ids as plain integers and try again."})
            continue
        state.n_steps += 1
        state.tokens += resp.usage.total_tokens
        state.model = resp.model or state.model
        state.messages.append(_assistant_message(resp))
        step = {"step": state.n_steps, "role": "assistant", "tokens": resp.usage.total_tokens}

        if not resp.tool_calls:
            step["text"] = _cut(resp.content or "")
            state.trace.append(step)
            if not state.text_fallback_used:  # try once to read a plan out of plain text
                state.text_fallback_used = True
                found = _extract_json_object(resp.content or "")
                if found is not None:
                    state.trace.append({"note": "no tool call; plan JSON extracted from the reply text"})
                    state.final_plan = found
                    return state
            state.messages.append({"role": "user", "content": "Please call submit_plan with your plan (or submit_no_plan)."})
            continue

        state.trace.append(step)
        for call in resp.tool_calls:
            entry: dict[str, Any] = {"tool": call.name, "args": _cut(call.arguments)}
            try:
                args = json.loads(call.arguments) if call.arguments.strip() else {}
            except ValueError:
                result: dict = {"error": "Arguments were not valid JSON. Send a JSON object."}
                args = None
            else:
                result = None  # filled below

            if args is not None:
                if call.name in ("submit_plan", "validate_plan"):
                    raw = args.get("plan", args if "actions" in args else None) if isinstance(args, dict) else None
                    if isinstance(raw, str):  # some models send the plan as a JSON string
                        try:
                            raw = json.loads(raw)
                        except ValueError:
                            pass
                    if raw is None:
                        result = {"error": "Missing 'plan' argument."}
                    elif call.name == "submit_plan":
                        candidate = raw if isinstance(raw, dict) else {"actions": raw}
                        problem = parse_plan(_force_ids(candidate, alert))
                        if isinstance(problem, Violation) and state.n_repairs < MAX_FORMAT_REPAIRS:
                            # Small models often slip on the format (a missing field, "new:N" instead of "new:1").
                            # Show the exact error and let the model fix it once. Not a rule violation, so nothing is hidden:
                            # if the second try is also unreadable it is stored as schema_invalid.
                            state.n_repairs += 1
                            result = {"error": problem.message[:700] + " Fix this and call submit_plan again. Use real values, e.g. temp_ref \"new:1\"."}
                            state.trace.append({"note": "plan had a format problem; sent back once for a fix"})
                        else:
                            state.final_plan = candidate
                            entry["result"] = "submitted"
                            state.trace.append(entry)
                            return state
                    elif state.n_validate >= MAX_VALIDATE_CALLS:
                        result = {"error": f"validate_plan can be called at most {MAX_VALIDATE_CALLS} times. Call submit_plan now."}
                    else:
                        state.n_validate += 1
                        result = _validate_for_agent(db, now, alert, raw, state)
                elif call.name == "submit_no_plan":
                    state.no_plan_reason = str(args.get("reason", "")) if isinstance(args, dict) else ""
                    entry["result"] = "no plan"
                    state.trace.append(entry)
                    return state
                else:
                    result = run_read_only_tool(db, now, call.name, args)

            entry["result"] = _cut(result)
            state.trace.append(entry)
            state.messages.append({"role": "tool", "tool_call_id": call.id, "content": json.dumps(result, separators=(",", ":"), default=str)})
    return state


# --- Turning a finished loop into a stored plan -----------------------------------------------------
def build_plan_row(
    db: Session, now: datetime, run: PlannerRun, alert: Alert, index: int, temperature: float, strategy: str,
    state: SampleState, latency_ms: int, seen_hashes: set[str],
) -> Plan:
    row = Plan(
        alert_id=alert.id, run_id=run.id, sample_index=index, temperature=temperature, strategy=strategy,
        status="proposed", is_valid=False, actions=[], rejection_reasons=[], trace=state.trace,
        draft_violations=state.draft_violations, n_steps=state.n_steps, n_validate_calls=state.n_validate,
        total_tokens=state.tokens, latency_ms=latency_ms, created_at=now, fabricated_ids=[],
    )
    if state.final_plan is None:
        if state.no_plan_reason is not None:
            row.outcome, row.rationale = "no_plan", state.no_plan_reason[:5000]
        else:
            row.outcome = "step_limit"
        return row

    raw = _force_ids(state.final_plan, alert, state)
    row.trace = state.trace  # _force_ids may have appended a note
    row.actions = raw.get("actions") if isinstance(raw.get("actions"), list) else []
    row.rationale = raw.get("rationale") if isinstance(raw.get("rationale"), str) else None
    row.plan_hash = compute_plan_hash(row.actions)
    row.fabricated_ids = find_fabricated_ids(db, raw)

    parsed = parse_plan(raw)
    if isinstance(parsed, Violation):
        row.outcome = "schema_invalid"
        row.rejection_reasons = [parsed.model_dump(mode="json")]
        row.checker_caught = None  # the checker never ran, so there is no catch to measure
    else:
        result = check_plan(db, parsed, now)  # the server's own verdict, never the agent's
        row.is_valid = result.is_valid
        row.outcome = "valid" if result.is_valid else "invalid"
        row.rejection_reasons = [v.model_dump(mode="json") for v in result.violations]
        row.checker_caught = checker_caught_all(row.fabricated_ids, result.violations)

    if row.plan_hash and row.plan_hash in seen_hashes:
        row.is_duplicate = True
    elif row.plan_hash:
        seen_hashes.add(row.plan_hash)
    return row


def _count_row(run: PlannerRun, row: Plan) -> None:
    """Update the run's counters. Duplicates are counted separately so they are not double-counted as distinct plans."""
    if row.is_duplicate:
        run.n_duplicates += 1
        return
    column = {
        "valid": "n_valid", "invalid": "n_invalid", "schema_invalid": "n_schema_invalid",
        "no_plan": "n_no_plan", "step_limit": "n_step_limit",
    }[row.outcome]
    setattr(run, column, getattr(run, column) + 1)


def _audit_plan(db: Session, now: datetime, row: Plan) -> None:
    log_event(db, now, "agent", "plan_proposed", "plan", row.id, {
        "run_id": row.run_id, "alert_id": row.alert_id, "sample_index": row.sample_index, "outcome": row.outcome,
        "is_valid": row.is_valid, "violation_codes": [v.get("code") for v in row.rejection_reasons],
        "fabricated_ids": row.fabricated_ids, "strategy": row.strategy, "temperature": row.temperature,
        "plan_hash": row.plan_hash, "is_duplicate": row.is_duplicate, "actions": summarise_actions(row.actions),
        "rationale": (row.rationale or "")[:RATIONALE_AUDIT_LIMIT],
    })


def _run_counts(run: PlannerRun) -> dict:
    return {
        "n_requested": run.n_requested, "n_valid": run.n_valid, "n_invalid": run.n_invalid,
        "n_schema_invalid": run.n_schema_invalid, "n_no_plan": run.n_no_plan, "n_step_limit": run.n_step_limit,
        "n_duplicates": run.n_duplicates, "n_llm_errors": run.n_llm_errors,
        "total_tokens": run.total_tokens, "duration_ms": run.duration_ms,
    }


# --- The whole run ----------------------------------------------------------------------------------
def execute_run(db: Session, run_id: int, llm: LLMClient) -> PlannerRun:
    """Run all samples of a queued run, one after another. Commits after every sample so the UI can show progress."""
    run = db.get(PlannerRun, run_id)
    alert = db.get(Alert, run.alert_id)
    now = get_sim_now(db)  # read once; the API refuses to move the clock while a run is active
    started = time.monotonic()
    run.status, run.started_at = "running", now
    log_event(db, now, "system", "planner_run_started", "planner_run", run.id, {
        "alert_id": alert.id, "course_id": alert.course_id, "n": run.n_requested, "model": run.model,
        "prompt_version": run.prompt_version, "shortfall_type": alert.shortfall_type,
    })
    db.commit()

    seen_hashes: set[str] = set()
    try:
        for index in range(run.n_requested):
            if index > 0 and settings.planner_sample_pause_seconds > 0:
                time.sleep(settings.planner_sample_pause_seconds)
            temperature = prompts.pick_temperature(index)
            strategy = prompts.pick_strategy(alert.shortfall_type, index)
            sample_started = time.monotonic()
            state = run_agent_loop(db, now, alert, llm, temperature, strategy)
            latency_ms = int((time.monotonic() - sample_started) * 1000)
            if state.model:
                run.model = state.model
            run.total_tokens += state.tokens
            row = build_plan_row(db, now, run, alert, index, temperature, strategy, state, latency_ms, seen_hashes)
            db.add(row)
            db.flush()
            _count_row(run, row)
            _audit_plan(db, now, row)
            run.duration_ms = int((time.monotonic() - started) * 1000)
            db.commit()
    except Exception as err:  # LLMError, or a bug: either way the run must not stay "running" forever
        db.rollback()
        kind = err.kind if isinstance(err, LLMError) else "internal"
        message = err.message if isinstance(err, LLMError) else f"{err.__class__.__name__}: {err}"
        run = db.get(PlannerRun, run_id)
        run.status, run.error = "failed", f"{kind}: {message}"
        run.n_llm_errors += 1 if kind != "internal" else 0
        run.finished_at = now
        run.duration_ms = int((time.monotonic() - started) * 1000)
        log_event(db, now, "system", "planner_run_failed", "planner_run", run.id, {**_run_counts(run), "error_kind": kind})
        db.commit()
        return run

    run.status, run.finished_at = "done", now
    run.duration_ms = int((time.monotonic() - started) * 1000)
    log_event(db, now, "system", "planner_run_finished", "planner_run", run.id, _run_counts(run))
    db.commit()
    return run


def run_planner(db: Session, alert_id: int, n: int, llm: LLMClient) -> PlannerRun:
    """Create and execute a run synchronously (used by tests and the smoke script; the API runs it in the background)."""
    alert = db.get(Alert, alert_id)
    run = create_run(db, alert, n, getattr(llm, "model", None) or settings.groq_model)
    return execute_run(db, run.id, llm)

