"""The planner loop, driven by a scripted FakeLLMClient (no network)."""
import hashlib
import json

import pytest
from sqlalchemy import func, select

from app.llm import prompts
from app.llm.base import LLMError
from app.llm.fake import FakeLLMClient, text_response, tool_response
from app.llm.planner import compute_plan_hash, run_planner
from app.llm.tools import run_read_only_tool
from app.models import Alert, AuditLog, Enrollment, Plan, PlannerRun, TrainingSession
from app.services.sim_clock import get_sim_now

ALERT_ID = 1  # capacity_gap on course 1 (see conftest.alert_template)


def good_plan(db, alert_id=ALERT_ID, how_many=2, skip=0):
    """A plan built only from tool output, so the checker accepts it."""
    now = get_sim_now(db)
    alert = db.get(Alert, alert_id)
    sessions = run_read_only_tool(db, now, "list_upcoming_sessions", {"course_id": alert.course_id, "min_free_seats": how_many})["sessions"]
    target = sessions[0]
    drivers = run_read_only_tool(db, now, "find_eligible_drivers", {"course_id": alert.course_id, "session_id": target["session_id"]})["drivers"]
    chosen = drivers[skip:skip + how_many]
    assert len(chosen) == how_many
    return {
        "course_id": alert.course_id, "alert_id": alert.id, "rationale": "Fill free seats.",
        "actions": [{"type": "enroll_driver", "driver_id": d["driver_id"], "session_id": target["session_id"]} for d in chosen],
    }


def submit(plan):
    return tool_response("submit_plan", {"plan": plan})


def plans_of(db, run):
    return list(db.scalars(select(Plan).where(Plan.run_id == run.id).order_by(Plan.sample_index)))


def test_happy_path_is_valid(adb):
    plan = good_plan(adb)
    llm = FakeLLMClient([tool_response("get_alert_context", {"alert_id": ALERT_ID}), submit(plan)])
    run = run_planner(adb, ALERT_ID, 1, llm)
    (row,) = plans_of(adb, run)
    assert run.status == "done" and run.n_valid == 1
    assert row.outcome == "valid" and row.is_valid and row.rejection_reasons == []
    assert row.rationale == "Fill free seats." and row.n_steps == 2 and row.fabricated_ids == [] and row.checker_caught is None
    assert row.status == "proposed" and row.score is None and row.rank is None and row.plan_hash
    assert run.total_tokens == 20 and run.prompt_version == prompts.PROMPT_VERSION and run.model == "fake"
    assert [t.get("tool") for t in row.trace if "tool" in t] == ["get_alert_context", "submit_plan"]


def test_fake_driver_id_is_caught_by_the_checker(adb):
    plan = good_plan(adb, how_many=1)
    plan["actions"].append({"type": "enroll_driver", "driver_id": 999999, "session_id": plan["actions"][0]["session_id"]})
    run = run_planner(adb, ALERT_ID, 1, FakeLLMClient([submit(plan)]))
    (row,) = plans_of(adb, run)
    assert row.outcome == "invalid" and not row.is_valid and run.n_invalid == 1
    assert "UNKNOWN_DRIVER" in [v["code"] for v in row.rejection_reasons]
    assert row.fabricated_ids == [{"type": "driver", "id": 999999}] and row.checker_caught is True


def test_fabricated_session_and_trainer_are_found(adb):
    course_id = adb.get(Alert, ALERT_ID).course_id
    plan = {"course_id": course_id, "actions": [
        {"type": "add_session", "temp_ref": "new:1", "course_id": course_id, "trainer_id": 4242,
         "start": "2026-06-07T09:00:00", "end": "2026-06-07T13:00:00", "location": "Room", "capacity": 5},
        {"type": "enroll_driver", "driver_id": 1, "session_id": 777777},
    ]}
    run = run_planner(adb, ALERT_ID, 1, FakeLLMClient([submit(plan)]))
    (row,) = plans_of(adb, run)
    kinds = {(f["type"], f["id"]) for f in row.fabricated_ids}
    assert kinds == {("trainer", 4242), ("session", 777777)} and row.checker_caught is True


def test_schema_invalid_plan_is_stored(adb):
    bad = submit({"course_id": 1, "actions": "enrol everyone"})
    llm = FakeLLMClient([bad, bad])  # the first attempt is sent back for a fix; the second is also unreadable
    run = run_planner(adb, ALERT_ID, 1, llm)
    (row,) = plans_of(adb, run)
    assert row.outcome == "schema_invalid" and run.n_schema_invalid == 1
    assert row.rejection_reasons[0]["code"] == "SCHEMA_INVALID" and row.actions == [] and row.checker_caught is None


def test_malformed_tool_arguments_get_an_error_and_the_loop_continues(adb):
    plan = good_plan(adb)
    llm = FakeLLMClient([tool_response("submit_plan", '{"plan": {oops'), submit(plan)])
    run = run_planner(adb, ALERT_ID, 1, llm)
    (row,) = plans_of(adb, run)
    assert row.outcome == "valid" and row.n_steps == 2
    tool_message = llm.requests[1]["messages"][-1]
    assert tool_message["role"] == "tool" and "not valid JSON" in tool_message["content"]


def test_plan_in_plain_text_is_extracted_once(adb):
    plan = good_plan(adb)
    llm = FakeLLMClient([text_response("Here is my plan:\n```json\n" + json.dumps(plan) + "\n```")])
    run = run_planner(adb, ALERT_ID, 1, llm)
    (row,) = plans_of(adb, run)
    assert row.outcome == "valid" and any("extracted" in t.get("note", "") for t in row.trace)


def test_agent_that_never_submits_hits_the_step_limit(adb, monkeypatch):
    monkeypatch.setattr("app.llm.planner.settings.planner_max_steps", 3)
    llm = FakeLLMClient([tool_response("get_alert_context", {"alert_id": ALERT_ID}) for _ in range(3)])
    run = run_planner(adb, ALERT_ID, 1, llm)
    (row,) = plans_of(adb, run)
    assert row.outcome == "step_limit" and row.n_steps == 3 and row.actions == [] and run.n_step_limit == 1 and run.status == "done"


def test_submit_no_plan(adb):
    run = run_planner(adb, ALERT_ID, 1, FakeLLMClient([tool_response("submit_no_plan", {"reason": "pool is empty"})]))
    (row,) = plans_of(adb, run)
    assert row.outcome == "no_plan" and row.rationale == "pool is empty" and run.n_no_plan == 1 and not row.is_valid


def test_second_validate_call_is_refused(adb):
    plan = good_plan(adb)
    validate = tool_response("validate_plan", {"plan": plan})
    llm = FakeLLMClient([validate, validate, submit(plan)])
    run = run_planner(adb, ALERT_ID, 1, llm)
    (row,) = plans_of(adb, run)
    assert row.n_validate_calls == 1 and len(row.draft_violations) == 1 and row.outcome == "valid"
    refusal = llm.requests[2]["messages"][-1]["content"]
    assert "at most 1 times" in refusal and "submit_plan" in refusal


def test_validate_reports_real_violations_to_the_agent(adb):
    plan = good_plan(adb, how_many=1)
    plan["actions"][0]["driver_id"] = 888888
    llm = FakeLLMClient([tool_response("validate_plan", {"plan": plan}), submit(plan)])
    run = run_planner(adb, ALERT_ID, 1, llm)
    (row,) = plans_of(adb, run)
    assert "UNKNOWN_DRIVER" in llm.requests[1]["messages"][-1]["content"]
    assert row.draft_violations == [{"call": 1, "is_valid": False, "codes": ["UNKNOWN_DRIVER"]}]
    assert row.outcome == "invalid"  # the server's own check, not the agent's decision to submit anyway


def test_wrong_course_and_alert_ids_are_overwritten(adb):
    plan = good_plan(adb)
    real_course = plan["course_id"]
    plan["course_id"], plan["alert_id"] = 999, 999
    run = run_planner(adb, ALERT_ID, 1, FakeLLMClient([submit(plan)]))
    (row,) = plans_of(adb, run)
    assert row.outcome == "valid" and row.alert_id == ALERT_ID
    notes = [t["note"] for t in row.trace if "note" in t]
    assert any("course_id=999" in n and str(real_course) in n for n in notes) and any("alert_id=999" in n for n in notes)


def test_identical_plans_are_flagged_as_duplicates(adb):
    plan = good_plan(adb)
    shuffled = dict(plan, actions=list(reversed(plan["actions"])))  # same actions, different order
    run = run_planner(adb, ALERT_ID, 2, FakeLLMClient([submit(plan), submit(shuffled)]))
    first, second = plans_of(adb, run)
    assert not first.is_duplicate and second.is_duplicate and first.plan_hash == second.plan_hash
    assert run.n_valid == 1 and run.n_duplicates == 1


def test_plan_hash_keeps_add_session_order():
    a = {"type": "add_session", "temp_ref": "new:1", "x": 1}
    b = {"type": "add_session", "temp_ref": "new:2", "x": 2}
    assert compute_plan_hash([a, b]) != compute_plan_hash([b, a])
    e1 = {"type": "enroll_driver", "driver_id": 1, "session_id": 5}
    e2 = {"type": "enroll_driver", "driver_id": 2, "session_id": 5}
    assert compute_plan_hash([e1, e2]) == compute_plan_hash([e2, e1])
    assert compute_plan_hash([]) is None and len(compute_plan_hash([e1])) == 64


def test_llm_error_mid_run_fails_the_run_and_keeps_earlier_plans(adb):
    plan = good_plan(adb)
    llm = FakeLLMClient([submit(plan), LLMError("timeout", "no answer")])
    run = run_planner(adb, ALERT_ID, 3, llm)
    assert run.status == "failed" and run.error.startswith("timeout") and run.n_llm_errors == 1
    assert len(plans_of(adb, run)) == 1 and run.n_valid == 1
    actions = [a.action for a in adb.scalars(select(AuditLog).where(AuditLog.entity_type == "planner_run"))]
    assert actions == ["planner_run_started", "planner_run_failed"]


def test_temperatures_and_strategies_per_sample(adb):
    alert = adb.get(Alert, ALERT_ID)
    assert alert.shortfall_type == "capacity_gap"
    llm = FakeLLMClient([tool_response("submit_no_plan", {"reason": "x"}) for _ in range(5)])
    run = run_planner(adb, ALERT_ID, 5, llm)
    assert [r["temperature"] for r in llm.requests] == [0.2, 0.5, 0.7, 0.9, 1.0]
    expected = prompts.STRATEGIES["capacity_gap"]
    for i, request in enumerate(llm.requests):
        assert expected[i % 3] in request["messages"][1]["content"]
    rows = plans_of(adb, run)
    assert [r.strategy for r in rows] == [expected[i % 3] for i in range(5)] and [r.temperature for r in rows] == [0.2, 0.5, 0.7, 0.9, 1.0]


def test_attendance_gap_strategies(adb):
    llm = FakeLLMClient([tool_response("submit_no_plan", {"reason": "x"}) for _ in range(3)])
    run_planner(adb, 6, 3, llm)
    for i, request in enumerate(llm.requests):
        assert prompts.STRATEGIES["attendance_gap"][i] in request["messages"][1]["content"]


def test_system_prompt_is_short_and_prefetched_facts_reach_the_model(adb):
    text = prompts.system_prompt()
    assert '"enroll_driver"' in text and "Only use IDs" in text
    assert len(text) < 3000  # roughly 800 tokens
    llm = FakeLLMClient([tool_response("submit_no_plan", {"reason": "x"})])
    run_planner(adb, ALERT_ID, 1, llm)
    user = llm.requests[0]["messages"][1]["content"]
    assert '"shortfall_type":"capacity_gap"' in user and '"sessions_with_free_seats"' in user


def test_planner_never_changes_sessions_or_enrollments(adb):
    def fingerprint():
        sessions = adb.execute(select(TrainingSession.id, TrainingSession.status, TrainingSession.capacity, TrainingSession.start_time).order_by(TrainingSession.id)).all()
        enrollments = adb.execute(select(Enrollment.id, Enrollment.session_id, Enrollment.driver_id, Enrollment.status).order_by(Enrollment.id)).all()
        return hashlib.sha256(repr((sessions, enrollments)).encode()).hexdigest()

    before = fingerprint()
    now_before = get_sim_now(adb)
    llm = FakeLLMClient([
        tool_response("get_alert_context", {"alert_id": ALERT_ID}), submit(good_plan(adb)),
        submit(good_plan(adb, how_many=1)), tool_response("submit_no_plan", {"reason": "x"}),
    ])
    run_planner(adb, ALERT_ID, 3, llm)
    assert fingerprint() == before and get_sim_now(adb) == now_before


def test_audit_trail(adb):
    plan = good_plan(adb)
    bad = submit({"course_id": 1, "actions": "bad"})
    llm = FakeLLMClient([submit(plan), bad, bad, tool_response("submit_no_plan", {"reason": "none"})])
    run = run_planner(adb, ALERT_ID, 3, llm)
    rows = list(adb.scalars(select(AuditLog).order_by(AuditLog.id)))
    by_action = {}
    for r in rows:
        by_action.setdefault(r.action, []).append(r)
    assert len(by_action["planner_run_started"]) == 1 and len(by_action["planner_run_finished"]) == 1
    assert len(by_action["plan_proposed"]) == 3
    started = by_action["planner_run_started"][0]
    assert started.actor == "system" and started.entity_id == run.id
    assert started.details["shortfall_type"] == "capacity_gap" and started.details["n"] == 3 and started.details["prompt_version"] == prompts.PROMPT_VERSION
    proposed = by_action["plan_proposed"]
    assert all(p.actor == "agent" and p.entity_type == "plan" for p in proposed)
    assert [p.details["outcome"] for p in proposed] == ["valid", "schema_invalid", "no_plan"]
    assert proposed[0].details["actions"] == "2 enrol" and proposed[0].details["rationale"] == "Fill free seats."
    finished = by_action["planner_run_finished"][0]
    assert finished.details["n_valid"] == 1 and finished.details["n_schema_invalid"] == 1 and finished.details["n_no_plan"] == 1
    assert "api_key" not in json.dumps([r.details for r in rows]).lower()


def test_run_row_counts_add_up(adb):
    plan = good_plan(adb)
    llm = FakeLLMClient([submit(plan), submit(plan), tool_response("submit_no_plan", {"reason": "x"})])
    run = run_planner(adb, ALERT_ID, 3, llm)
    total = run.n_valid + run.n_invalid + run.n_schema_invalid + run.n_no_plan + run.n_step_limit + run.n_duplicates + run.n_llm_errors
    assert total == run.n_requested == 3
    assert adb.scalar(select(func.count()).select_from(PlannerRun)) == 1


def test_planner_pauses_between_samples_but_not_before_the_first(adb, monkeypatch):
    sleeps = []
    monkeypatch.setattr("app.llm.planner.settings.planner_sample_pause_seconds", 2.5)
    monkeypatch.setattr("app.llm.planner.time.sleep", sleeps.append)
    llm = FakeLLMClient([tool_response("submit_no_plan", {"reason": "x"}) for _ in range(3)])
    run_planner(adb, ALERT_ID, 3, llm)
    assert sleeps == [2.5, 2.5]


def test_old_tool_results_are_pruned_to_stay_under_budget(adb, monkeypatch):
    from app.llm.planner import OMITTED_NOTE, TOOLS_CHARS, prune_messages

    big = "x" * 3000
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    for i in range(4):
        messages += [{"role": "assistant", "content": None, "tool_calls": [{"id": str(i)}]}, {"role": "tool", "tool_call_id": str(i), "content": big}]
    budget = TOOLS_CHARS + 7000
    assert prune_messages(messages, budget) == 2  # oldest two dropped, newest two kept
    tool_texts = [m["content"] for m in messages if m["role"] == "tool"]
    assert tool_texts[:2] == [OMITTED_NOTE, OMITTED_NOTE] and tool_texts[2:] == [big, big]
    assert prune_messages(messages, budget) == 0  # already under budget


def test_loop_prunes_and_still_finishes(adb, monkeypatch):
    monkeypatch.setattr("app.llm.planner.settings.planner_context_char_budget", 1)  # force pruning every step
    plan = good_plan(adb)
    llm = FakeLLMClient([tool_response("get_alert_context", {"alert_id": ALERT_ID}), tool_response("list_upcoming_sessions", {"course_id": 1}), submit(plan)])
    run = run_planner(adb, ALERT_ID, 1, llm)
    (row,) = plans_of(adb, run)
    assert row.outcome == "valid" and any("dropped from the prompt" in t.get("note", "") for t in row.trace)
    assert "omitted" in llm.requests[2]["messages"][3]["content"]


def test_provider_rejecting_a_tool_call_is_retried_not_fatal(adb):
    plan = good_plan(adb)
    err = LLMError("bad_request", "Tool call validation failed: /session_id: expected integer, but got string")
    run = run_planner(adb, ALERT_ID, 1, FakeLLMClient([err, submit(plan)]))
    (row,) = plans_of(adb, run)
    assert run.status == "done" and row.outcome == "valid" and row.n_steps == 2
    assert any("rejected the tool call" in t.get("note", "") for t in row.trace)


def test_other_bad_requests_still_fail_the_run(adb):
    run = run_planner(adb, ALERT_ID, 1, FakeLLMClient([LLMError("bad_request", "model does not exist")]))
    assert run.status == "failed" and run.error.startswith("bad_request")


def test_a_format_mistake_is_sent_back_once_and_can_be_fixed(adb):
    plan = good_plan(adb)
    broken = {"course_id": plan["course_id"], "actions": [{"type": "add_session", "temp_ref": "new:N"}]}
    llm = FakeLLMClient([submit(broken), submit(plan)])
    run = run_planner(adb, ALERT_ID, 1, llm)
    (row,) = plans_of(adb, run)
    assert row.outcome == "valid" and row.n_steps == 2 and run.n_schema_invalid == 0
    feedback = llm.requests[1]["messages"][-1]["content"]
    assert "does not match the required format" in feedback and "submit_plan again" in feedback
    assert any("sent back once" in t.get("note", "") for t in row.trace)


def test_the_prompt_shows_a_concrete_example_and_never_the_letter_n():
    text = prompts.system_prompt()
    assert '"temp_ref":"new:1"' in text and '"new:N"' not in text and "never the letter N" in text
