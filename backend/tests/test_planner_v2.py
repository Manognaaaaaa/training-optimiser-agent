"""Planner v2: time-aware tools, repair and prune, diversity, the rule-based baseline, honest "no plan". No network."""
import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select, update

from app.config import settings
from app.llm.fake import FakeLLMClient, tool_response
from app.llm.fix_hints import FIX_HINTS
from app.llm.planner import overlaps_earlier, plan_signature, run_planner
from app.llm.tools import run_read_only_tool, tool_specs
from app.models import Alert, Driver, Plan, Trainer, TrainingSession
from app.routers.planner import metrics
from app.schemas.plan import parse_plan
from app.services.constraints import CATEGORY, check_plan
from app.services.feasibility import Feasibility, assess
from app.services.greedy_planner import build_greedy_plan
from app.services.planner_policy import earliest_new_session_start
from app.services.ranking import build_context
from app.services.sim_clock import get_sim_now
from tests.test_planner import ALERT_ID, good_plan, plans_of, submit


def first_window(adb, hour, min_drivers=1, **kw):
    """The first allowed window starting at this hour with enough drivers and a free trainer."""
    alert = adb.get(Alert, ALERT_ID)
    now = get_sim_now(adb)
    feas = Feasibility(adb, adb.get(type(alert).course.property.mapper.class_, alert.course_id) if False else _course(adb, alert), now)
    first = earliest_new_session_start(now).date()
    for w in feas.windows(first, first + timedelta(days=14), hours=(hour,)):
        if w.count >= min_drivers and w.free_trainers:
            return feas, w
    raise AssertionError("no such window")


def _course(adb, alert):
    from app.models import Course
    return adb.get(Course, alert.course_id)


# --- Tools ------------------------------------------------------------------------------------------------
def test_night_only_pool_cannot_start_in_the_morning(adb):
    adb.execute(update(Driver).values(shift="night"))
    adb.commit()
    alert = adb.get(Alert, ALERT_ID)
    now = get_sim_now(adb)
    feas = Feasibility(adb, _course(adb, alert), now)
    first = earliest_new_session_start(now).date()
    morning = next(d for d in (first + timedelta(days=i) for i in range(14)) if d.weekday() in (6, 0, 1, 2, 3))
    start = datetime.combine(morning, datetime.min.time()).replace(hour=9)

    result = run_read_only_tool(adb, now, "find_eligible_drivers", {"course_id": alert.course_id, "start": start.isoformat()})
    assert result["drivers"] == [] and result["total_eligible"] == 0
    assert result["excluded_counts"].get("NIGHT_SHIFT_MORNING", 0) > 0
    assert result["pool_summary"]["pool_by_shift"] == {"night": result["pool_summary"]["pool_size"]}
    assert "nationality" not in json.dumps(result)

    windows = run_read_only_tool(adb, now, "suggest_session_times", {"course_id": alert.course_id})
    assert windows["windows"], "afternoon and evening windows should exist"
    for w in windows["windows"]:
        assert datetime.fromisoformat(w["start"]).hour >= 12  # never a morning start
        assert w["available_driver_count"] > 0 and w["qualified_trainers_free"] and w["room_free"] is True
        again = run_read_only_tool(adb, now, "find_eligible_drivers", {"course_id": alert.course_id, "start": w["start"], "limit": 25})
        assert again["total_eligible"] == w["available_driver_count"]  # the suggestion and the driver tool agree
    assert len(windows["windows"]) <= 8 and feas.pool


def test_find_eligible_drivers_asks_for_a_time_not_a_temp_ref(adb):
    alert = adb.get(Alert, ALERT_ID)
    now = get_sim_now(adb)
    err = run_read_only_tool(adb, now, "find_eligible_drivers", {"course_id": alert.course_id, "session_id": "new:1"})
    assert "pass start" in err["error"]
    assert "error" in run_read_only_tool(adb, now, "find_eligible_drivers", {"course_id": alert.course_id})
    too_soon = (now + timedelta(hours=5)).isoformat()
    assert "at least" in run_read_only_tool(adb, now, "find_eligible_drivers", {"course_id": alert.course_id, "start": too_soon})["error"]
    wrong_length = (earliest_new_session_start(now) + timedelta(days=1)).replace(hour=14, minute=0, second=0)
    err = run_read_only_tool(adb, now, "find_eligible_drivers", {
        "course_id": alert.course_id, "start": wrong_length.isoformat(), "end": (wrong_length + timedelta(hours=1)).isoformat()})
    assert "lasts" in err["error"]


def test_existing_session_lookup_still_works_and_reports_exclusions(adb):
    alert = adb.get(Alert, ALERT_ID)
    now = get_sim_now(adb)
    session = run_read_only_tool(adb, now, "list_upcoming_sessions", {"course_id": alert.course_id, "min_free_seats": 1})["sessions"][0]
    result = run_read_only_tool(adb, now, "find_eligible_drivers", {"course_id": alert.course_id, "session_id": session["session_id"]})
    assert result["session_id"] == session["session_id"] and result["drivers"] and isinstance(result["excluded_counts"], dict)
    shifts = {d["shift"] for d in result["drivers"]}
    assert len(shifts) > 1  # the short list mixes shifts instead of being the first few ids


def test_alert_context_has_pool_and_hours_by_shift(adb):
    ctx = run_read_only_tool(adb, get_sim_now(adb), "get_alert_context", {"alert_id": ALERT_ID})
    assert ctx["pool"]["size"] == sum(ctx["pool"]["by_shift"].values()) > 0
    night = ctx["start_hours_that_work_by_shift"]["night"]
    assert night["earliest_start"] == "12:00" and "09:00" not in [f"{h:02d}:00" for h in night["start_hours_that_work"]]
    assert ctx["start_hours_that_work_by_shift"]["day"]["earliest_start"] is not None
    assert ctx["sessions_with_free_seats"] and "pool_drivers_who_can_attend" in ctx["sessions_with_free_seats"][0]
    assert ctx["min_lead_hours_for_new_sessions"] == settings.planner_min_lead_hours


def test_validate_tool_description_states_the_configured_limit(monkeypatch):
    def description():
        return next(t["function"]["description"] for t in tool_specs() if t["function"]["name"] == "validate_plan")
    assert f"At most {settings.planner_max_validate_calls} calls" in description()
    monkeypatch.setattr("app.config.settings.planner_max_validate_calls", 5)
    assert "At most 5 calls" in description()
    assert "suggest_session_times" in [t["function"]["name"] for t in tool_specs()]


def test_every_checker_code_has_a_fix_hint():
    missing = [code for code in CATEGORY if code not in FIX_HINTS]
    assert missing == []
    assert "12:00" in FIX_HINTS["NIGHT_SHIFT_MORNING"] and "8h" in FIX_HINTS["DRIVER_ON_SHIFT"]


# --- Lead time (planner policy, not a checker rule) ----------------------------------------------------------------
def test_lead_time_is_a_planner_policy_not_a_checker_rule(adb):
    alert = adb.get(Alert, ALERT_ID)
    now = get_sim_now(adb)
    slots = run_read_only_tool(adb, now, "find_trainer_slots", {"course_id": alert.course_id, "limit": 1})["slots"]
    assert datetime.fromisoformat(slots[0]["start"]) >= earliest_new_session_start(now)  # the tool only offers allowed slots

    soon = (now + timedelta(days=1)).replace(hour=14, minute=0)  # tomorrow afternoon: far less than 48h away
    end = soon + timedelta(hours=4)
    plan = {"course_id": alert.course_id, "actions": [
        {"type": "add_session", "temp_ref": "new:1", "course_id": alert.course_id, "trainer_id": 1, "start": soon.isoformat(),
         "end": end.isoformat(), "location": "Training Room", "capacity": 5}]}
    assert check_plan(adb, parse_plan(plan), now).is_valid  # the checker itself has no objection
    run = run_planner(adb, ALERT_ID, 1, FakeLLMClient([submit(plan)]))
    (row,) = [p for p in plans_of(adb, run) if p.source == "llm"]
    assert row.outcome == "invalid" and [v["code"] for v in row.rejection_reasons] == ["PLANNER_LEAD_TIME"]
    assert row.rejection_reasons[0]["category"] == "policy"


# --- Repair loop and prune ------------------------------------------------------------------------------------------------
def morning_plan_with_night_drivers(adb):
    """A new 09:00 session with 4 drivers who pass and 2 night drivers who cannot attend a morning session."""
    feas, window = first_window(adb, 9, min_drivers=4)
    alert = adb.get(Alert, ALERT_ID)
    good = window.available[:4]
    night = [d.id for d in feas.pool if d.shift == "night"][:2]
    assert len(night) == 2 and not set(night) & set(window.available)
    actions = [{"type": "add_session", "temp_ref": "new:1", "course_id": alert.course_id, "trainer_id": window.free_trainers[0],
                "start": window.start.isoformat(), "end": window.end.isoformat(), "location": "Training Room", "capacity": 8}]
    actions += [{"type": "enroll_driver", "driver_id": d, "session_id": "new:1"} for d in good + night]
    return {"course_id": alert.course_id, "alert_id": alert.id, "rationale": "A morning session.", "actions": actions}, good, night


def test_repair_loop_gives_fix_hints_and_prune_keeps_the_valid_part(adb):
    plan, good, night = morning_plan_with_night_drivers(adb)
    llm = FakeLLMClient([tool_response("validate_plan", {"plan": plan}), submit(plan)])
    run = run_planner(adb, ALERT_ID, 1, llm)

    feedback = json.loads(llm.requests[1]["messages"][-1]["content"])
    assert feedback["is_valid"] is False
    codes = {v["code"] for v in feedback["violations"]}
    assert "NIGHT_SHIFT_MORNING" in codes
    first = feedback["violations"][0]
    assert set(first) == {"code", "action_index", "driver_id", "session_ref", "fix_hint"} and first["driver_id"] in night
    assert "12:00" in next(v["fix_hint"] for v in feedback["violations"] if v["code"] == "NIGHT_SHIFT_MORNING")

    rows = plans_of(adb, run)
    raw = next(p for p in rows if p.source == "llm")
    pruned = next(p for p in rows if p.source == "llm_pruned")
    assert raw.outcome == "invalid" and raw.actions == plan["actions"]  # the raw model output is stored untouched
    assert pruned.outcome == "valid" and pruned.is_valid and pruned.parent_plan_id == raw.id and pruned.status == "valid"
    kept = {a["driver_id"] for a in pruned.actions if a["type"] == "enroll_driver"}
    assert kept == set(good) and len(pruned.actions) == 5
    assert pruned.fabricated_ids == [] and pruned.checker_caught is None
    assert run.n_pruned == 1 and run.n_invalid == 1 and run.n_valid == 0  # run counters describe the raw LLM output
    assert any("prune step" in t.get("note", "") for t in raw.trace)
    assert pruned.rank is not None  # the pruned plan competes in the ranking

    m = metrics(adb)
    assert m["plans_total"] == 1 and m["plans_by_source"] == {"llm": 1, "llm_pruned": 1}  # hallucination metrics: raw output only


def test_prune_drops_a_new_session_left_too_small(adb):
    plan, good, night = morning_plan_with_night_drivers(adb)
    plan["actions"] = [a for a in plan["actions"] if a.get("driver_id") not in good[1:]]  # only 1 passing driver + 2 night drivers
    run = run_planner(adb, ALERT_ID, 1, FakeLLMClient([submit(plan)]))
    rows = plans_of(adb, run)
    assert [p.source for p in rows] == ["llm"] and rows[0].outcome == "invalid"  # nothing valid and big enough was left


def test_prune_gives_up_on_whole_plan_problems(adb):
    plan = good_plan(adb, how_many=1)
    plan["actions"].append({"type": "enroll_driver", "driver_id": 999999, "session_id": plan["actions"][0]["session_id"]})
    run = run_planner(adb, ALERT_ID, 1, FakeLLMClient([submit(plan)]))
    rows = plans_of(adb, run)
    raw = next(p for p in rows if p.source == "llm")
    assert raw.fabricated_ids == [{"type": "driver", "id": 999999}] and raw.checker_caught is True
    assert all(p.source != "llm_pruned" for p in rows)  # one real booking is worth under 1 expected completion


# --- Diversity -------------------------------------------------------------------------------------------------------------------
def test_signature_overlap_marks_near_copies_as_duplicates(adb):
    a = {"actions": [{"type": "enroll_driver", "driver_id": i, "session_id": 5} for i in range(1, 7)]}
    b = {"actions": [{"type": "enroll_driver", "driver_id": i, "session_id": 5} for i in (1, 2, 3, 4, 5, 9)]}  # 5 of 6 shared
    c = {"actions": [{"type": "enroll_driver", "driver_id": i, "session_id": 5} for i in (1, 2, 3, 20, 21, 22)]}  # 3 of 6 shared
    sig = plan_signature
    assert overlaps_earlier(sig(b["actions"]), [sig(a["actions"])]) is True
    assert overlaps_earlier(sig(c["actions"]), [sig(a["actions"])]) is False
    assert overlaps_earlier(set(), [sig(a["actions"])]) is False


def test_a_near_copy_sample_is_a_duplicate_and_not_ranked(adb):
    first = good_plan(adb, how_many=6)
    copy = json.loads(json.dumps(first))
    alert = adb.get(Alert, ALERT_ID)
    now = get_sim_now(adb)
    session_id = first["actions"][0]["session_id"]
    spare = run_read_only_tool(adb, now, "find_eligible_drivers", {"course_id": alert.course_id, "session_id": session_id, "limit": 25})["drivers"]
    used = {a["driver_id"] for a in first["actions"]}
    copy["actions"][-1]["driver_id"] = next(d["driver_id"] for d in spare if d["driver_id"] not in used)  # 5 of 6 drivers shared
    run = run_planner(adb, ALERT_ID, 2, FakeLLMClient([submit(first), submit(copy)]))
    rows = plans_of(adb, run)
    assert [p.is_duplicate for p in rows] == [False, True] and run.n_duplicates == 1 and run.n_valid == 1
    assert rows[1].status == "superseded" and rows[1].rank is None and rows[0].rank == 1


def test_later_samples_are_told_what_earlier_ones_proposed(adb):
    first = good_plan(adb, how_many=2)
    llm = FakeLLMClient([submit(first), tool_response("submit_no_plan", {"reason": "x"})])
    run_planner(adb, ALERT_ID, 2, llm)
    second_prompt = next(r["messages"][1]["content"] for r in llm.requests if "Earlier attempts" in r["messages"][1]["content"])
    assert "do something different" in second_prompt
    assert str(first["actions"][0]["driver_id"]) in second_prompt


# --- Rule-based baseline ------------------------------------------------------------------------------------------------------
def test_greedy_plan_is_valid_on_every_feasible_alert(adb):
    alerts = list(adb.scalars(select(Alert).where(Alert.status == "open").order_by(Alert.id)))
    assert len(alerts) >= 4
    checked = 0
    for alert in alerts[:5]:
        now = get_sim_now(adb)
        feas = Feasibility(adb, _course(adb, alert), now)
        verdict = assess(feas)
        raw = build_greedy_plan(adb, alert, build_context(adb, alert, now), feas)
        if not verdict["feasible"]:
            continue
        assert raw is not None, f"alert {alert.id}: feasible, but the baseline found nothing"
        parsed = parse_plan(raw)
        assert not hasattr(parsed, "code"), parsed
        from app.llm.repair import all_violations
        assert all_violations(adb, parsed, now) == [], f"alert {alert.id}"
        checked += 1
    assert checked >= 4


def test_every_run_stores_the_baseline_next_to_the_llm_samples(adb, monkeypatch):
    monkeypatch.setattr("app.config.settings.planner_greedy_baseline", True)
    run = run_planner(adb, ALERT_ID, 1, FakeLLMClient([tool_response("submit_no_plan", {"reason": "x"})]))
    rows = plans_of(adb, run)
    baseline = next(p for p in rows if p.source == "greedy_baseline")
    assert baseline.outcome == "valid" and baseline.is_valid and baseline.rank == 1 and baseline.fabricated_ids == []
    assert run.n_greedy == 1 and run.n_valid == 0 and run.n_no_plan == 1  # raw LLM counters exclude the baseline
    assert adb.get(Alert, ALERT_ID).status == "plans_ready"
    from app.routers.planner import run_to_dict
    assert run_to_dict(adb, run)["samples_done"] == 1  # the baseline is not a sample
    spread = {a["driver_id"] for a in baseline.actions if a["type"] == "enroll_driver"}
    assert len(spread) >= 2


# --- Honest "no plan" ------------------------------------------------------------------------------------------------------------
def test_infeasible_alert_skips_the_llm_and_says_why(adb):
    adb.execute(update(Driver).values(is_active=False, hire_date=datetime(2020, 1, 1).date()))
    adb.commit()
    llm = FakeLLMClient([])
    run = run_planner(adb, ALERT_ID, 3, llm)
    assert llm.requests == []  # zero LLM calls
    assert run.status == "done" and run.infeasibility["code"] == "pool_empty" and run.infeasibility["numbers"]["pool_size"] == 0
    alert = adb.get(Alert, ALERT_ID)
    assert alert.status == "no_valid_plans" and plans_of(adb, run) == []
    assert run.infeasibility["message"] in alert.message  # the written alert states the reason


def test_assess_reports_no_trainer_when_drivers_exist_but_nobody_can_teach(adb):
    alert = adb.get(Alert, ALERT_ID)
    now = get_sim_now(adb)
    adb.execute(update(Trainer).values(max_sessions_per_week=0))
    adb.execute(update(TrainingSession).where(TrainingSession.course_id == alert.course_id, TrainingSession.start_time > now).values(status="cancelled"))
    adb.commit()
    verdict = assess(Feasibility(adb, _course(adb, alert), now))
    assert verdict["feasible"] is False and verdict["code"] == "no_trainer_free"
    assert verdict["numbers"]["windows_with_drivers"] > 0 and verdict["numbers"]["usable_new_session_windows"] == 0


def test_feasible_alert_is_not_short_circuited(adb):
    verdict = assess(Feasibility(adb, _course(adb, adb.get(Alert, ALERT_ID)), get_sim_now(adb)))
    assert verdict["feasible"] is True and verdict["numbers"]["usable_new_session_windows"] > 0
