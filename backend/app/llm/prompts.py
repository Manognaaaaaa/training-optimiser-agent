"""Prompt text for the planning agent. Plain strings, versioned, so every stored plan says which prompt made it."""
import json

from app.schemas.plan import Plan

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


def _plan_schema_text() -> str:
    """The exact plan format, generated from the real pydantic model so it can never drift."""
    return json.dumps(Plan.model_json_schema(), separators=(",", ":"))


def system_prompt() -> str:
    return f"""You are a planning agent for a driver training department. A course is at risk of missing its annual completion target. Propose ONE redistribution plan that closes the gap, using only real data from the tools.

Action types (put them in "actions"):
- enroll_driver: book a driver into a session {{"type":"enroll_driver","driver_id":int,"session_id":int|"new:N"}}
- move_driver: move a booked driver from one session to another {{"type":"move_driver","driver_id":int,"from_session_id":int,"to_session_id":int|"new:N"}}
- add_session: create a session {{"type":"add_session","temp_ref":"new:N","course_id":int,"trainer_id":int,"start":"YYYY-MM-DDTHH:MM:00","end":"YYYY-MM-DDTHH:MM:00","location":str,"capacity":int}}
A new session must come BEFORE any action that uses its temp_ref (new:1, new:2, ...).

Plan JSON schema (JSON Schema):
{_plan_schema_text()}

Hard rules (a checker rejects any plan that breaks one):
- Never exceed a session's capacity. A new session's capacity is at most the course default capacity.
- Never book a driver who is unavailable, on shift, active=false, already trained in this course this year, or already booked for this course.
- Shifts: no session during or soon after a driver's shift; night-shift drivers cannot start before 12:00 the morning after.
- A driver gets at most one session per day and at least 12 hours of rest between sessions.
- A trainer cannot teach two overlapping sessions, nor exceed their weekly session limit.
- Sessions that already started cannot be changed. New sessions must start after the current time, run Sunday to Thursday, start at 09:00, 14:00 or 18:00, and last as long as the slot tools show.

How to work:
1. Call get_alert_context first, then look things up with the other tools.
2. Only use IDs returned by tools. Never invent a driver, session or trainer ID. Copy start/end times exactly as returned.
3. Prefer drivers returned by find_eligible_drivers and slots returned by find_trainer_slots; they already pass the rules.
4. Call validate_plan on your draft (at most 2 times), fix what it reports, then call submit_plan.
5. Include a short "rationale" (1-3 sentences) in the plan. If no valid fix exists, call submit_no_plan with the reason.
Be efficient: do not call tools you do not need."""


def sample_prompt(alert_id: int, course_id: int, strategy: str) -> str:
    return (
        f"Alert {alert_id} for course {course_id} is open. "
        f"Approach for this attempt: {strategy}. "
        "Start with get_alert_context, then build and submit a plan."
    )
