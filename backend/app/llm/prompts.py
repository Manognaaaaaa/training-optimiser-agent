"""Prompt text for the planning agent. Plain strings, versioned, so every stored plan says which prompt made it."""
import json  # noqa: F401

PROMPT_VERSION = "p5-v1"

# Strategy hints, rotated per sample so the N plans differ. Keyed by the alert's shortfall type.
STRATEGIES: dict[str, list[str]] = {
    "capacity_gap": [
        "add sessions and fill them",
        "add fewer, larger sessions",
        "mix new sessions with filling existing free seats",
    ],
    "attendance_gap": [
        "fill free seats in upcoming sessions with extra eligible drivers",
        "move drivers away from sessions that clash with their shift into ones that fit",
        "prioritise sessions inside the booking window",
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


def pick_strategy(shortfall_type: str | None, sample_index: int) -> str:
    options = strategies_for(shortfall_type)
    return options[sample_index % len(options)]


def pick_temperature(sample_index: int) -> float:
    return TEMPERATURES[sample_index % len(TEMPERATURES)]


def system_prompt() -> str:
    """Kept deliberately short: Groq's free tier allows few tokens per minute and every step re-sends this text."""
    return """You plan driver-training changes for a course that is at risk of missing its annual target. Return ONE plan built only from real data.

Plan JSON, every field is required. Example with one new session and two bookings:
{"course_id":3,"rationale":"Adds a session and fills a free seat.","actions":[
{"type":"add_session","temp_ref":"new:1","course_id":3,"trainer_id":2,"start":"2026-04-05T09:00:00","end":"2026-04-05T13:00:00","location":"Training Room","capacity":10},
{"type":"enroll_driver","driver_id":17,"session_id":"new:1"},
{"type":"enroll_driver","driver_id":18,"session_id":5}]}
Other action: {"type":"move_driver","driver_id":18,"from_session_id":5,"to_session_id":"new:1"}.
temp_ref is literally new:1, then new:2 and so on (a number, never the letter N). add_session comes before any action that uses its temp_ref, and needs all 8 fields including course_id.

Rules (a checker rejects any violation): never exceed session capacity (new sessions: at most the course default); only active drivers who are available, off shift, not yet trained in this course this year and not already booked for it; one session per driver per day with 12h rest; trainers cannot overlap or exceed their weekly limit; no past sessions; new sessions run Sunday-Thursday starting 09:00, 14:00 or 18:00.

Only use IDs and times returned by tools or given below. The facts below usually contain everything you need (eligible drivers per session, open slots for new sessions): build the plan from them and call submit_plan right away. Call other tools only if the facts are not enough. If nothing valid exists, call submit_no_plan. Be brief; use few tool calls."""


def sample_prompt(alert_id: int, course_id: int, strategy: str, facts: str = "") -> str:
    text = f"Alert {alert_id}, course {course_id}. Approach for this attempt: {strategy}."
    if facts:
        text += "\nFacts already looked up for you (real data):\n" + facts
    return text
