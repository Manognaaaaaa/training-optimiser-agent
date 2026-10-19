"""Phase 6: the plan scorer and the ranking that runs at the end of a planner run. No network (FakeLLMClient)."""
import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.config import Settings, settings
from app.llm.fake import FakeLLMClient, tool_response
from app.llm.planner import run_planner
from app.models import Alert, AuditLog, Plan
from app.services.ranking import build_context, rank_plans, score_plan, sort_key, weights
from app.services.sim_clock import get_sim_now
from tests.test_planner import ALERT_ID, good_plan, submit


def run_with(adb, plans, extra=()):
    """Run the planner with one scripted sample per plan; ``extra`` replies are consumed by the alert writer."""
    return run_planner(adb, ALERT_ID, len(plans), FakeLLMClient([submit(p) for p in plans] + list(extra)))


def test_weights_sum_to_one_and_config_refuses_otherwise():
    assert sum(weights().values()) == pytest.approx(1.0)
    with pytest.raises(ValidationError):
        Settings(score_weight_closure=0.5)


def test_components_for_a_hand_built_plan(adb):
    alert = adb.get(Alert, ALERT_ID)
    ctx = build_context(adb, alert)
    plan = good_plan(adb, how_many=2)
    b = score_plan(ctx, alert.course_id, plan["actions"])

    assert b["drivers_moved"] == 2 and b["sessions_added"] == 0 and b["trainer_hours_added"] == 0
    # Two new seats, each worth about the course show-up rate.
    assert b["expected_gain"] == pytest.approx(2 * ctx.fc.p_show, abs=0.1)
    assert b["components"]["closure"] == pytest.approx(min(1, b["expected_gain"] / b["shortfall_gap"]), abs=0.01)
    assert b["components"]["disruption"] == pytest.approx(1 - 2 / settings.disruption_cap)
    assert b["components"]["resource"] == 1.0
    assert b["p_hit_after"] >= b["p_hit_before"]
    assert b["scoring_version"] == "v1" and b["weights"] == weights()
    expected = sum(weights()[k] * v for k, v in b["components"].items())
    assert b["score_total"] == pytest.approx(expected, abs=1e-3)
    assert all(0 <= v <= 1 for v in b["components"].values())


def test_new_session_costs_trainer_hours_and_disruption(adb):
    from app.llm.tools import run_read_only_tool
    alert = adb.get(Alert, ALERT_ID)
    now = get_sim_now(adb)
    slot = run_read_only_tool(adb, now, "find_trainer_slots", {"course_id": alert.course_id, "limit": 1})["slots"][0]
    actions = [{"type": "add_session", "temp_ref": "new:1", "course_id": alert.course_id, "trainer_id": slot["trainer_id"],
                "start": slot["start"], "end": slot["end"], "location": "Room", "capacity": 10}]
    b = score_plan(build_context(adb, alert), alert.course_id, actions)
    assert b["sessions_added"] == 1 and b["trainer_hours_added"] > 0
    assert b["components"]["disruption"] == pytest.approx(1 - 2 / settings.disruption_cap)
    assert b["components"]["resource"] < 1.0
    assert b["expected_gain"] == 0  # an empty new session has no booked seats yet


def test_plan_that_burdens_one_group_gets_lower_fairness_and_a_flag(adb):
    alert = adb.get(Alert, ALERT_ID)
    ctx = build_context(adb, alert)
    session = next(s for s in ctx.facts.sessions_of(alert.course_id) if s.status == "scheduled" and s.start_time >= ctx.now)

    def plan_for(drivers):
        return [{"type": "enroll_driver", "driver_id": d.id, "session_id": session.id} for d in drivers]

    pool = sorted(ctx.pool, key=lambda d: d.id)
    night = [d for d in pool if d.shift == "night"][:5]
    mixed = []
    for shift in ("day", "night", "rotating"):
        mixed += [d for d in pool if d.shift == shift][:2]
    night_share = sum(d.shift == "night" for d in pool) / len(pool)
    assert night_share < 0.6  # the scenario only makes sense if night drivers are a minority of the pool

    skewed = score_plan(ctx, alert.course_id, plan_for(night))
    balanced = score_plan(ctx, alert.course_id, plan_for(mixed))
    assert skewed["fairness_flag"] is True and balanced["fairness_flag"] is False
    assert skewed["components"]["fairness"] < balanced["components"]["fairness"]
    assert skewed["fairness_groups"]["shift"]["burden"] == {"night": 1.0}


def test_sort_key_breaks_ties_by_closure_then_fewer_drivers_then_hash():
    def b(total, closure, moved):
        return {"score_total": total, "components": {"closure": closure}, "drivers_moved": moved}

    items = [("b", b(0.5, 0.4, 3)), ("a", b(0.5, 0.4, 3)), ("c", b(0.5, 0.4, 2)), ("d", b(0.5, 0.6, 9)), ("e", b(0.7, 0.1, 9))]
    ordered = [h for h, br in sorted(items, key=lambda i: sort_key(i[1], i[0]))]
    assert ordered == ["e", "d", "c", "a", "b"]


def test_run_ranks_valid_plans_only_and_leaves_invalid_ones_alone(adb):
    bad = good_plan(adb, how_many=1)
    bad["actions"].append({"type": "enroll_driver", "driver_id": 999999, "session_id": bad["actions"][0]["session_id"]})
    plans = [good_plan(adb, how_many=1), good_plan(adb, how_many=2), bad, good_plan(adb, how_many=3)]
    run = run_with(adb, plans)
    rows = list(adb.scalars(select(Plan).where(Plan.run_id == run.id).order_by(Plan.sample_index)))

    ranked = [p for p in rows if p.rank is not None]
    assert sorted(p.rank for p in ranked) == [1, 2, 3]
    scores_by_rank = [p.score_total for p in sorted(ranked, key=lambda p: p.rank)]
    assert scores_by_rank == sorted(scores_by_rank, reverse=True)
    invalid = rows[2]
    assert invalid.status == "invalid" and invalid.rank is None and invalid.score_total is None and invalid.rejection_reasons
    assert all(p.status == "valid" and p.scoring_version == "v1" and p.score == p.score_total for p in ranked)
    assert adb.get(Alert, ALERT_ID).status == "plans_ready"

    audit = adb.scalars(select(AuditLog).where(AuditLog.action == "plans_ranked", AuditLog.entity_id == run.id)).one()
    assert [r["plan_id"] for r in audit.details["ranking"]] == [p.id for p in sorted(ranked, key=lambda p: p.rank)]
    assert audit.details["weights"] == weights()


def test_ranking_twice_gives_the_same_order(adb):
    run = run_with(adb, [good_plan(adb, how_many=1), good_plan(adb, how_many=2), good_plan(adb, how_many=3)])
    first = [(p.id, p.rank) for p in adb.scalars(select(Plan).where(Plan.run_id == run.id)) if p.rank]
    rank_plans(adb, run.id)
    adb.commit()
    second = [(p.id, p.rank) for p in adb.scalars(select(Plan).where(Plan.run_id == run.id)) if p.rank]
    assert sorted(first) == sorted(second)


def test_no_valid_plans_sets_the_alert_status(adb):
    run = run_planner(adb, ALERT_ID, 1, FakeLLMClient([tool_response("submit_no_plan", {"reason": "nothing to do"})]))
    assert run.status == "done"
    alert = adb.get(Alert, ALERT_ID)
    assert alert.status == "no_valid_plans" and alert.message  # a template message explains the situation
