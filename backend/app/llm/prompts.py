"""Prompt text for the planning agent. Plain strings, versioned, so every stored plan says which prompt made it."""
import json  # noqa: F401

from app.config import settings

PROMPT_VERSION = "p6-v2"

# Strategy hints, rotated per sample so the N plans differ. Keyed by the alert's shortfall type.
# "{largest_shift}" is replaced by the biggest shift group in the eligible pool (see pick_strategy).
STRATEGIES: dict[str, list[str]] = {
    "capacity_gap": [
        "fill existing low-fill sessions only, no new sessions",
        "one new afternoon or evening session aimed at the {largest_shift} shift group in the pool",
        "split across two new sessions on different days to spread trainer load",
        "mix: fill existing sessions first, then one new session for whoever is left",
    ],
    "attendance_gap": [
        "fill free seats in upcoming sessions with extra eligible drivers",
        "move drivers away from sessions that clash with their shift into ones that fit",
        "prioritise sessions inside the booking window",
        "mix: fill existing sessions first, then one new session aimed at the {largest_shift} shift group",
    ],
    "pool_gap": [
        "enrol eligible drivers who have no booking yet",
        "spread new bookings across several sessions",
    ],
}
DEFAULT_STRATEGIES = ["use whatever mix of actions fits the shortfall best"]

TEMPERATURES = [0.2, 0.5, 0.7, 0.9, 1.0]


def strategies_for(shortfall_type: str | None) -> list[str]:
    return STRATEGIES.get(shortfall_type or "", DEFAULT_STRATEGIES)


def pick_strategy(shortfall_type: str | None, sample_index: int, pool_by_shift: dict[str, int] | None = None) -> str:
    """The approach for one sample. Different per sample, and aimed at the biggest shift group in the pool when it matters."""
    options = strategies_for(shortfall_type)
    text = options[sample_index % len(options)]
    if "{largest_shift}" in text:
        largest = max(pool_by_shift, key=pool_by_shift.get) if pool_by_shift else "largest"
        text = text.replace("{largest_shift}", largest)
    return text


def pick_temperature(sample_index: int) -> float:
    return TEMPERATURES[sample_index % len(TEMPERATURES)]


def system_prompt() -> str:
    """Kept deliberately short: Groq's free tier allows few tokens per minute and every step re-sends this text."""
    lead = settings.planner_min_lead_hours
    return f"""You plan driver-training changes for a course that is at risk of missing its annual target. Return ONE plan built only from real data.

Plan JSON, every field is required. Example with one new session and two bookings:
{{"course_id":3,"rationale":"Adds a session and fills a free seat.","actions":[
{{"type":"add_session","temp_ref":"new:1","course_id":3,"trainer_id":2,"start":"2026-04-05T14:00:00","end":"2026-04-05T18:00:00","location":"Training Room","capacity":8}},
{{"type":"enroll_driver","driver_id":17,"session_id":"new:1"}},
{{"type":"enroll_driver","driver_id":18,"session_id":5}}]}}
Other action: {{"type":"move_driver","driver_id":18,"from_session_id":5,"to_session_id":"new:1"}}.
temp_ref is literally new:1, then new:2 and so on (a number, never the letter N). add_session comes before any action that uses its temp_ref, and needs all 8 fields including course_id.

Work in this order:
1. Read the pool by shift and the start hours that work for each shift in the facts. Night-shift drivers cannot start before 12:00.
2. Existing sessions with free seats: use find_eligible_drivers(session_id) and book drivers it returns.
3. New session: call suggest_session_times, choose a window from its output, then call find_eligible_drivers with that exact start. Use the trainer ids it lists.
4. Call validate_plan, fix every violation using its fix_hint, then submit_plan.

RULE: never assign a driver to a session time you have not checked with find_eligible_drivers for that exact time. Only assign drivers it returned.

The checker rejects: exceeding capacity (new sessions: at most the course default), inactive, unavailable, on-shift or already-trained drivers, double bookings, one session per driver per day with 12h rest, trainer overlap or weekly limit, past sessions. New sessions run Sunday-Thursday starting 09:00, 14:00 or 18:00 and at least {lead}h from now.

Only use IDs and times returned by tools or given in the facts. If nothing valid exists, call submit_no_plan. Be brief."""


def sample_prompt(alert_id: int, course_id: int, strategy: str, facts: str = "", already: str = "") -> str:
    text = f"Alert {alert_id}, course {course_id}. Approach for this attempt: {strategy}."
    if already:
        text += "\nEarlier attempts already proposed this (do something different): " + already
    if facts:
        text += "\nFacts already looked up for you (real data):\n" + facts
    return text
