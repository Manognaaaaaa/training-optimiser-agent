"""Writes the trainer-facing alert text. The LLM only writes words; the ranking comes from ``ranking.py``.

Flow (see docs/ranking_and_alerts.md):
1. ``build_fact_pack``: the only thing the LLM sees, built from the database in code.
2. The LLM answers with JSON that is validated with Pydantic (``AlertMessageOut``).
3. ``ground_check`` verifies, deterministically, that every number, id, date and name in the text is in
   the fact pack and that the recommended plan is the rank 1 plan.
4. One retry that lists the problems; if that also fails, a template built only from fact pack values.

Every attempt is stored in ``alert_messages`` (the Phase 8 hallucination metrics read it) and one summary
row goes to the audit log.
"""
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.llm.base import LLMClient
from app.models import Alert, AlertMessage, Driver, Plan, PlannerRun, Trainer
from app.services.audit import log_event
from app.services.plan_view import describe_actions
from app.services.ranking import ScoringContext

PROMPT_VERSION = "alert_writer_v1"
PROMPT_PATH = Path(__file__).resolve().parents[1] / "llm" / "prompt_files" / "alert_writer_v1.txt"
MAX_LLM_ATTEMPTS = 2
MAX_TOKENS = 1200
HEADLINE_MAX = 90


# --- The message format ----------------------------------------------------------------------------
class PlanText(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: int
    rationale: str = Field(min_length=1, max_length=600)
    tradeoffs: str = Field(min_length=1, max_length=400)


class AlertMessageOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    headline: str = Field(min_length=1, max_length=HEADLINE_MAX)
    summary: str = Field(min_length=1, max_length=1200)
    why_at_risk: list[str] = Field(max_length=8)
    plans: list[PlanText]
    recommended_plan_id: int
    caveats: list[str] = Field(default_factory=list, max_length=6)


def system_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def prompt_hash() -> str:
    return hashlib.sha256(system_prompt().encode()).hexdigest()[:16]


# --- Fact pack ---------------------------------------------------------------------------------------
def _change_text(c: dict) -> str:
    """One readable line per change, using only database values."""
    if c["type"] == "add_session":
        return (f"Add new session {c['temp_ref']} on {c['when']} with trainer {c['trainer_name']} "
                f"at {c['location']} (capacity {c['capacity']})")
    who = f"{c['driver_name']} (driver {c['driver_id']})"
    if c["type"] == "enroll_driver":
        return f"Enrol {who} into session {c['session_id']} ({c['when']})"
    return (f"Move {who} from session {c['from_session_id']} ({c['from_when']}) "
            f"to session {c['to_session_id']} ({c['to_when']})")


def build_fact_pack(db: Session, alert: Alert, presented: list[Plan], ctx: ScoringContext) -> dict:
    """Everything the LLM may talk about. Numbers are rounded here so the LLM can copy them as they are."""
    fc = ctx.fc
    details = alert.details or {}
    plans = []
    for plan in presented:
        b = plan.score_breakdown
        changes = describe_actions(db, plan.actions)
        plans.append({
            "plan_id": plan.id, "rank": plan.rank, "score_total": round(plan.score_total, 2),
            "score_components": {k: round(v, 2) for k, v in b["components"].items()},
            "chance_of_hitting_target_before_pct": round(b["p_hit_before"] * 100),
            "chance_of_hitting_target_after_pct": round(b["p_hit_after"] * 100),
            "drivers_moved": b["drivers_moved"], "sessions_added": b["sessions_added"],
            "fairness_flag": b["fairness_flag"],
            "changes": [_change_text(c) for c in changes],
        })
    return {
        "course": {"id": alert.course_id, "code": fc.code, "name": fc.name},
        "sim_date": ctx.now.date().isoformat(),
        "target_completions": fc.target,
        "completed_to_date": fc.attended,
        "forecast_completions": round(fc.projected),
        "forecast_range_90pct": {"low": round(fc.low), "high": round(fc.high)},
        "chance_of_hitting_target_pct": round(fc.p_hit * 100),
        "risk_level": alert.risk_level,
        "shortfall_type": alert.shortfall_type,
        "reasons": [r.get("message", "") for r in details.get("reasons", [])][:5],
        "plans": plans,
    }


# --- Grounding check ---------------------------------------------------------------------------------
NUMBER_RE = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?")
NAME_RE = re.compile(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+\b")
WORD_RE = re.compile(r"[A-Za-z]+")
CALENDAR_WORDS = {
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "mon", "tue", "wed", "thu", "fri", "sat", "sun", "january", "february", "march", "april", "may", "june", "july",
    "august", "september", "october", "november", "december", "jan", "feb", "mar", "apr", "jun", "jul", "aug",
    "sep", "sept", "oct", "nov", "dec",
}
GENERIC_WORDS = {
    "risk", "plan", "plans", "course", "session", "sessions", "driver", "drivers", "trainer", "trainers", "target",
    "forecast", "high", "medium", "low", "capacity", "gap", "attendance", "pool",
}
# Capitalised words that start sentences; they are dropped from the front of a candidate name.
SENTENCE_STARTERS = {
    "the", "this", "that", "these", "those", "both", "each", "all", "plan", "plans", "course", "moving", "adding",
    "enrolling", "enrol", "move", "add", "if", "it", "with", "without", "because", "however", "but", "and", "there",
    "our", "we", "a", "an", "in", "on", "at", "to", "for", "from", "by", "as", "so", "one", "two", "three", "high",
    "medium", "low", "risk", "session", "sessions", "driver", "drivers", "trainer", "trainers", "no", "some", "most",
}


def _numbers_in(text: str) -> list[str]:
    return NUMBER_RE.findall(text)


def _to_float(token: str) -> float:
    return float(token.replace(",", ""))


def _walk(value: Any, numbers: set[float], words: set[str]) -> None:
    """Collect every number and word that appears anywhere in the fact pack."""
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, (int, float)):
        numbers.add(float(value))
        if 0 <= value <= 1:
            numbers.add(float(value) * 100)  # a fraction may be quoted as a percentage
    elif isinstance(value, str):
        numbers.update(_to_float(t) for t in _numbers_in(value))
        words.update(w.lower() for w in WORD_RE.findall(value))
    elif isinstance(value, dict):
        for k, v in value.items():
            words.update(w.lower() for w in WORD_RE.findall(str(k)))
            _walk(v, numbers, words)
    elif isinstance(value, list):
        for v in value:
            _walk(v, numbers, words)


def _text_fields(msg: AlertMessageOut) -> list[tuple[str, str]]:
    fields = [("headline", msg.headline), ("summary", msg.summary)]
    fields += [(f"why_at_risk[{i}]", t) for i, t in enumerate(msg.why_at_risk)]
    for i, p in enumerate(msg.plans):
        fields += [(f"plans[{i}].rationale", p.rationale), (f"plans[{i}].tradeoffs", p.tradeoffs)]
    fields += [(f"caveats[{i}]", t) for i, t in enumerate(msg.caveats)]
    return fields


def ground_check(msg: AlertMessageOut, pack: dict, known_names: set[str] | None = None) -> list[dict]:
    """Return a list of ``{field, token, reason}``; empty means the message is grounded.

    ``known_names`` are all real driver and trainer names in the database: one that is not in the fact pack
    must not be mentioned. Names that are in neither are caught by the capitalised-words heuristic.
    """
    issues: list[dict] = []
    presented = {p["plan_id"]: p["rank"] for p in pack["plans"]}

    if presented:
        rank_one = next((pid for pid, rank in presented.items() if rank == 1), None)
        if msg.recommended_plan_id != rank_one:
            issues.append({"field": "recommended_plan_id", "token": str(msg.recommended_plan_id),
                           "reason": f"must be the rank 1 plan ({rank_one})"})
    seen: set[int] = set()
    for i, p in enumerate(msg.plans):
        if p.plan_id not in presented:
            issues.append({"field": f"plans[{i}].plan_id", "token": str(p.plan_id), "reason": "not a plan in the fact pack"})
        elif p.plan_id in seen:
            issues.append({"field": f"plans[{i}].plan_id", "token": str(p.plan_id), "reason": "plan described twice"})
        seen.add(p.plan_id)
    for missing in sorted(set(presented) - seen):
        issues.append({"field": "plans", "token": str(missing), "reason": "plan missing from the message"})

    numbers: set[float] = {90.0}  # "90% range" is a fixed label
    words: set[str] = set()
    _walk(pack, numbers, words)
    numbers.update(float(i) for i in range(0, len(presented) + 1))  # "2 plans", "plan 1"
    vocabulary = words | CALENDAR_WORDS | GENERIC_WORDS
    pack_text = json.dumps(pack).lower()

    for field_name, text in _text_fields(msg):
        for token in _numbers_in(text):
            x = _to_float(token)
            decimals = len(token.split(".")[1]) if "." in token else 0
            tolerance = 0.5 * 10 ** -decimals + 1e-6
            if not any(abs(x - a) <= tolerance for a in numbers):
                issues.append({"field": field_name, "token": token, "reason": "number is not in the fact pack"})
        for name in known_names or ():
            if name.lower() in text.lower() and name.lower() not in pack_text:
                issues.append({"field": field_name, "token": name, "reason": "person is not in the fact pack"})
        for candidate in NAME_RE.findall(text):
            parts = candidate.split()
            while parts and parts[0].lower() in SENTENCE_STARTERS:
                parts.pop(0)
            if len(parts) >= 2 and any(w.lower() not in vocabulary for w in parts):
                issues.append({"field": field_name, "token": " ".join(parts), "reason": "name is not in the fact pack"})
    # The same problem found twice (for example a person caught by both checks) is reported once.
    unique = {(i["field"], i["token"], i["reason"]): i for i in issues}
    return list(unique.values())


# --- Fallback template -------------------------------------------------------------------------------
def template_message(pack: dict) -> AlertMessageOut:
    """A plain message built only from fact pack values. Used when the LLM fails twice, or when there are no plans."""
    course = pack["course"]["name"]
    rng = pack["forecast_range_90pct"]
    summary = (
        f"{course} has {pack['completed_to_date']} of {pack['target_completions']} completions so far and is forecast "
        f"to finish at {pack['forecast_completions']} (90% range {rng['low']} to {rng['high']}). "
        f"The chance of hitting the target is {pack['chance_of_hitting_target_pct']}%."
    )
    why = [r for r in pack["reasons"] if r][:3] or [f"Shortfall type: {pack['shortfall_type']}."]
    plans = []
    for p in pack["plans"]:
        plans.append(PlanText(
            plan_id=p["plan_id"],
            rationale=(f"Ranked {p['rank']} with a score of {p['score_total']}. It raises the chance of hitting the target "
                       f"from {p['chance_of_hitting_target_before_pct']}% to {p['chance_of_hitting_target_after_pct']}%."),
            tradeoffs=f"It touches {p['drivers_moved']} driver(s) and adds {p['sessions_added']} session(s).",
        ))
    if pack["plans"]:
        caveats = []
    else:
        caveats = ["No plan passed the checks yet. Try generating plans again."]
    best = next((p["plan_id"] for p in pack["plans"] if p["rank"] == 1), 0)
    return AlertMessageOut(
        headline=f"{course}: {pack['risk_level']} risk of missing the target"[:HEADLINE_MAX],
        summary=summary, why_at_risk=why, plans=plans, recommended_plan_id=best, caveats=caveats,
    )


# --- Calling the LLM ---------------------------------------------------------------------------------
def _parse(raw: str) -> tuple[AlertMessageOut | None, str]:
    """Pull the JSON object out of the reply and validate it. Returns (message, error)."""
    start = raw.find("{")
    if start == -1:
        return None, "the reply contains no JSON object"
    try:
        data, _ = json.JSONDecoder().raw_decode(raw[start:])
    except ValueError as err:
        return None, f"the reply is not valid JSON ({err})"
    try:
        return AlertMessageOut.model_validate(data), ""
    except ValidationError as err:
        first = err.errors()[0]
        return None, f"the JSON does not match the format: {'.'.join(str(p) for p in first['loc'])}: {first['msg']}"


def _user_prompt(pack: dict, issues: list[dict] | None) -> str:
    text = "FACT PACK:\n" + json.dumps(pack, separators=(",", ":"))
    if issues:
        listed = "; ".join(f"{i['field']}: '{i['token']}' ({i['reason']})" for i in issues[:12])
        text += f"\n\nYour previous answer was rejected for these problems: {listed}. Fix them and answer again with the JSON object only."
    return text


def _known_names(db: Session) -> set[str]:
    names = set(db.scalars(select(Driver.name))) | set(db.scalars(select(Trainer.name)))
    return {n for n in names if n and len(n) >= 5}


def write_alert_message(
    db: Session, llm: LLMClient, alert: Alert, run: PlannerRun | None, presented: list[Plan], ctx: ScoringContext,
) -> AlertMessage:
    """Write (and ground-check) the alert text. Stores one row per attempt and returns the final one. Does not commit."""
    now = ctx.now
    pack = build_fact_pack(db, alert, presented, ctx)
    names = _known_names(db)
    model = getattr(llm, "model", "") or settings.groq_model
    run_id = run.id if run else None
    rows: list[AlertMessage] = []
    issues: list[dict] | None = None
    final: AlertMessage | None = None

    attempts = MAX_LLM_ATTEMPTS if presented else 0  # with no plans there is nothing for the LLM to rank or explain
    for attempt in range(1, attempts + 1):
        messages = [{"role": "system", "content": system_prompt()}, {"role": "user", "content": _user_prompt(pack, issues)}]
        started = time.monotonic()
        raw, error = "", ""
        try:
            resp = llm.chat(messages, temperature=settings.alert_writer_temperature, max_tokens=MAX_TOKENS,
                            response_format={"type": "json_object"})
            raw = resp.content or ""
            model = resp.model or model
        except Exception as err:  # any LLM failure means "no usable text"; the template covers it
            error = f"LLM call failed: {err}"
        latency = int((time.monotonic() - started) * 1000)

        parsed: AlertMessageOut | None = None
        if not error:
            parsed, error = _parse(raw)
        found = [{"field": "reply", "token": "", "reason": error}] if error else ground_check(parsed, pack, names)
        row = AlertMessage(
            alert_id=alert.id, planner_run_id=run_id, model=model, prompt_version=PROMPT_VERSION, raw_output=raw,
            parsed=parsed.model_dump() if parsed else {}, grounding_ok=not found, grounding_issues=found,
            used_fallback=False, attempts=attempt, latency_ms=latency, created_at=now,
        )
        db.add(row)
        rows.append(row)
        if not found:
            final = row
            break
        issues = found

    if final is None:  # the LLM failed twice (or there were no plans): deterministic template
        template = template_message(pack)
        found = ground_check(template, pack, names)
        final = AlertMessage(
            alert_id=alert.id, planner_run_id=run_id, model="template", prompt_version=PROMPT_VERSION, raw_output="",
            parsed=template.model_dump(), grounding_ok=not found, grounding_issues=found, used_fallback=True,
            attempts=attempts, latency_ms=0, created_at=now,
        )
        db.add(final)
        rows.append(final)

    db.flush()
    shown = AlertMessageOut.model_validate(final.parsed)
    alert.message = f"{shown.headline}\n{shown.summary}"
    log_event(db, now, "agent", "alert_message_written", "alert", alert.id, {
        "message_id": final.id, "model": model, "prompt_version": PROMPT_VERSION, "prompt_hash": prompt_hash(),
        "grounding_ok": final.grounding_ok, "used_fallback": final.used_fallback, "attempts": attempts,
        "attempt_results": [{"attempt": r.attempts, "fallback": r.used_fallback, "grounding_ok": r.grounding_ok,
                             "issues": len(r.grounding_issues)} for r in rows],
        "run_id": run_id,
    })
    return final
