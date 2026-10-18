"""The agent's read-only tools, run against a seeded year played to 2026-05-21 (no LLM involved)."""
import json
from datetime import datetime

from sqlalchemy import select

from app.llm.tools import MAX_TOOL_ROWS, READ_ONLY_TOOLS, TOOL_SPECS, run_read_only_tool
from app.models import Course, Driver, Trainer, TrainingSession
from app.schemas.plan import parse_plan
from app.services.constraints import check_plan
from app.services.sim_clock import get_sim_now

ALERT_ID = 1  # capacity_gap


def tool(db, name, **args):
    return run_read_only_tool(db, get_sim_now(db), name, args)


def course_of(db, alert_id=ALERT_ID):
    return tool(db, "get_alert_context", alert_id=alert_id)["course"]["id"]


def sessions_with_free_seats(db, course_id, count=4):
    out = tool(db, "list_upcoming_sessions", course_id=course_id, min_free_seats=1)["sessions"]
    assert out, "the seeded year should have upcoming sessions with free seats"
    return out[:count]


def collect_ids(value, found=None):
    """Every value stored under a *_id key anywhere in a tool result."""
    found = found if found is not None else {}
    if isinstance(value, dict):
        for key, item in value.items():
            if key.endswith("_id") and isinstance(item, int):
                found.setdefault(key, set()).add(item)
            collect_ids(item, found)
    elif isinstance(value, list):
        for item in value:
            collect_ids(item, found)
    return found


def test_alert_context(adb):
    ctx = tool(adb, "get_alert_context", alert_id=ALERT_ID)
    assert ctx["shortfall_type"] == "capacity_gap" and ctx["status"] == "open"
    assert ctx["gap"] > 0 and ctx["booking_window_days"] == 21 and ctx["sim_now"].startswith("2026-05-21")
    assert ctx["course"]["default_capacity"] > 0 and ctx["reasons"]


def test_every_returned_id_exists(adb):
    course_id = course_of(adb)
    session = sessions_with_free_seats(adb, course_id, 1)[0]
    results = [
        tool(adb, "get_alert_context", alert_id=ALERT_ID),
        tool(adb, "list_upcoming_sessions", course_id=course_id),
        tool(adb, "get_session_roster", session_id=session["session_id"]),
        tool(adb, "find_eligible_drivers", course_id=course_id, session_id=session["session_id"]),
        tool(adb, "find_trainer_slots", course_id=course_id),
    ]
    tables = {"driver_id": Driver, "session_id": TrainingSession, "trainer_id": Trainer, "course_id": Course}
    for result in results:
        for key, ids in collect_ids(result).items():
            if key not in tables:  # alert_id is checked by get_alert_context itself
                continue
            real = set(adb.scalars(select(tables[key].id).where(tables[key].id.in_(ids))))
            assert ids <= real, f"{key} {ids - real} does not exist"


def test_eligible_drivers_all_pass_the_checker(adb):
    course_id = course_of(adb)
    checked = 0
    for session in sessions_with_free_seats(adb, course_id, 5):
        result = tool(adb, "find_eligible_drivers", course_id=course_id, session_id=session["session_id"], limit=10)
        assert "error" not in result
        for driver in result["drivers"]:
            plan = parse_plan({"course_id": course_id, "actions": [
                {"type": "enroll_driver", "driver_id": driver["driver_id"], "session_id": session["session_id"]}]})
            verdict = check_plan(adb, plan)
            assert verdict.is_valid, verdict.violations
            checked += 1
    assert checked > 0


def test_eligible_drivers_for_a_move_pass_the_checker(adb):
    course_id = course_of(adb)
    sessions = tool(adb, "list_upcoming_sessions", course_id=course_id)["sessions"]
    source = next(s for s in sessions if s["booked"] > 0)
    target = next(s for s in sessions if s["free_seats"] > 0 and s["session_id"] != source["session_id"])
    result = tool(adb, "find_eligible_drivers", course_id=course_id, session_id=target["session_id"],
                  move_from_session_id=source["session_id"], limit=25)
    assert "error" not in result
    for driver in result["drivers"]:
        plan = parse_plan({"course_id": course_id, "actions": [{
            "type": "move_driver", "driver_id": driver["driver_id"],
            "from_session_id": source["session_id"], "to_session_id": target["session_id"]}]})
        assert check_plan(adb, plan).is_valid


def test_trainer_slots_all_pass_an_add_session_check(adb):
    course_id = course_of(adb)
    course = adb.get(Course, course_id)
    result = tool(adb, "find_trainer_slots", course_id=course_id, limit=10)
    assert result["slots"]
    for slot in result["slots"]:
        start = datetime.fromisoformat(slot["start"])
        assert start.weekday() in (6, 0, 1, 2, 3) and start.hour in (9, 14, 18)
        plan = parse_plan({"course_id": course_id, "actions": [{
            "type": "add_session", "temp_ref": "new:1", "course_id": course_id, "trainer_id": slot["trainer_id"],
            "start": slot["start"], "end": slot["end"], "location": "Training Room", "capacity": course.default_capacity}]})
        verdict = check_plan(adb, plan)
        assert verdict.is_valid, verdict.violations


def test_no_tool_output_contains_nationality(adb):
    course_id = course_of(adb)
    session = sessions_with_free_seats(adb, course_id, 1)[0]
    for name, args in [
        ("get_session_roster", {"session_id": session["session_id"]}),
        ("find_eligible_drivers", {"course_id": course_id, "session_id": session["session_id"]}),
        ("list_upcoming_sessions", {"course_id": course_id}),
    ]:
        assert "nationalit" not in json.dumps(tool(adb, name, **args)).lower()
    assert "nationalit" not in json.dumps(TOOL_SPECS).lower()


def test_bad_arguments_return_an_error_dict(adb):
    assert "error" in tool(adb, "get_alert_context")  # missing alert_id
    assert "error" in tool(adb, "get_alert_context", alert_id="abc")
    assert "error" in tool(adb, "get_alert_context", alert_id=99999)
    assert "error" in tool(adb, "get_session_roster", session_id=99999)
    assert "error" in tool(adb, "list_upcoming_sessions", course_id=99999)
    assert "error" in tool(adb, "find_trainer_slots", course_id=1, from_date="2026-05-22", to_date="2026-12-01")
    assert "error" in tool(adb, "no_such_tool")
    assert "error" in run_read_only_tool(adb, get_sim_now(adb), "get_alert_context", "not a dict")


def test_eligible_drivers_rejects_session_of_another_course(adb):
    course_id = course_of(adb)
    other = adb.scalar(select(TrainingSession).where(TrainingSession.course_id != course_id, TrainingSession.status == "scheduled"))
    assert "error" in tool(adb, "find_eligible_drivers", course_id=course_id, session_id=other.id)


def test_results_are_capped(adb):
    course_id = course_of(adb)
    slots = tool(adb, "find_trainer_slots", course_id=course_id, limit=1000)
    assert len(slots["slots"]) <= MAX_TOOL_ROWS
    assert slots["truncated"] == (slots["total_matching"] > MAX_TOOL_ROWS)
    sessions = tool(adb, "list_upcoming_sessions", course_id=course_id)
    assert len(sessions["sessions"]) <= MAX_TOOL_ROWS
    assert sessions["truncated"] == (sessions["total_matching"] > MAX_TOOL_ROWS)


def test_tools_never_write(adb):
    course_id = course_of(adb)
    session = sessions_with_free_seats(adb, course_id, 1)[0]
    tool(adb, "find_eligible_drivers", course_id=course_id, session_id=session["session_id"])
    tool(adb, "find_trainer_slots", course_id=course_id)
    assert not adb.new and not adb.dirty


def test_every_registered_tool_has_a_spec():
    spec_names = {s["function"]["name"] for s in TOOL_SPECS}
    assert set(READ_ONLY_TOOLS) <= spec_names
    assert {"validate_plan", "submit_plan", "submit_no_plan"} <= spec_names
