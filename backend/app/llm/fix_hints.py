"""One short repair instruction per violation code, shown to the agent next to each violation from validate_plan.

A small model reads "move this session to start at or after 12:00, or remove this driver" far better than a long
sentence about rest rules. Every checker code must have a hint (a test checks that).
"""
FIX_HINTS: dict[str, str] = {
    # hallucination: ids that do not exist
    "UNKNOWN_DRIVER": "remove this action; use only driver ids returned by find_eligible_drivers",
    "UNKNOWN_SESSION": "remove this action; use only session ids returned by list_upcoming_sessions",
    "UNKNOWN_TRAINER": "use a trainer id returned by suggest_session_times",
    "UNKNOWN_COURSE": "use the course id of this alert",
    "UNKNOWN_TEMP_REF": "add the add_session action with this temp_ref BEFORE any action that uses it",
    "NOT_ENROLLED": "this driver is not in that session; use enroll_driver instead of move_driver, or pick a driver from get_session_roster",
    # validity
    "INACTIVE_DRIVER": "remove this driver; only use drivers returned by find_eligible_drivers",
    "SESSION_NOT_SCHEDULED": "use a session that is still scheduled (list_upcoming_sessions)",
    "SESSION_IN_PAST": "choose a session or a start time later than now",
    "WRONG_COURSE": "use only sessions of this alert's course",
    "ALREADY_COMPLETED": "remove this driver; they already finished this course this year",
    "ALREADY_BOOKED": "remove this driver; they are already booked for this course",
    "BAD_SESSION_TIMES": "end must be after start, in 2026, and the session must last exactly the course's session length",
    "BAD_CAPACITY": "capacity must be between 1 and the course's default capacity",
    "DUPLICATE_TEMP_REF": "give every add_session its own temp_ref: new:1, new:2, ...",
    # capacity
    "OVER_CAPACITY": "book fewer drivers into this session, or raise capacity (new sessions only, up to the course maximum)",
    # availability
    "DRIVER_UNAVAILABLE": "remove this driver (leave or sickness at that time) or move the session to another time",
    "DRIVER_ON_SHIFT": "pick a time with at least 8h after this driver's shift ends, or remove this driver",
    # double booking
    "DRIVER_DOUBLE_BOOKED": "remove this driver or pick a time that does not overlap their other session",
    "TRAINER_DOUBLE_BOOKED": "pick another trainer from suggest_session_times for this time, or another time",
    # trainer load
    "TRAINER_WEEKLY_LOAD": "pick another trainer, or put the session in a week where this trainer has capacity",
    # rest
    "MAX_SESSIONS_PER_DAY": "remove this driver from one of their two sessions that day",
    "MIN_REST_GAP": "remove this driver or pick a time at least 12h away from their other session",
    "NIGHT_SHIFT_MORNING": "move this session to start at or after 12:00, or remove this driver",
    # planner policy (not a checker rule)
    "PLANNER_LEAD_TIME": "start the new session later; new sessions must begin at least the minimum lead time from now",
    # schema
    "SCHEMA_INVALID": "fix the JSON so it matches the plan format exactly",
}

DEFAULT_HINT = "remove the action that causes this, or change it"


def hint_for(code: str) -> str:
    return FIX_HINTS.get(code, DEFAULT_HINT)
