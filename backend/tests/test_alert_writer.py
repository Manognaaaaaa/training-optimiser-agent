"""Phase 6: the alert writer, its grounding check, the retry and the fallback. No network (FakeLLMClient)."""
import json

import pytest
from sqlalchemy import select

from app.llm.fake import FakeLLMClient, text_response
from app.llm.planner import run_planner
from app.models import Alert, AlertMessage, AuditLog, Driver, Plan
from app.services.alert_writer import (
    AlertMessageOut, PlanText, build_fact_pack, ground_check, template_message, write_alert_message,
)
from app.services.ranking import build_context
from tests.test_planner import ALERT_ID, good_plan, submit


@pytest.fixture()
def ranked(adb):
    """A finished run with two ranked plans (the alert writer fell back to the template because the script ran out)."""
    llm = FakeLLMClient([submit(good_plan(adb, how_many=1)), submit(good_plan(adb, how_many=2))])
    run = run_planner(adb, ALERT_ID, 2, llm)
    plans = sorted(adb.scalars(select(Plan).where(Plan.run_id == run.id, Plan.rank.is_not(None))), key=lambda p: p.rank)
    alert = adb.get(Alert, ALERT_ID)
    ctx = build_context(adb, alert)
    return adb, alert, run, plans, ctx, build_fact_pack(adb, alert, plans, ctx)


def good_message(pack) -> AlertMessageOut:
    return template_message(pack)


def with_change(msg: AlertMessageOut, **changes) -> AlertMessageOut:
    return AlertMessageOut.model_validate({**msg.model_dump(), **changes})


def test_template_message_passes_its_own_grounding_check(ranked):
    _, _, _, plans, _, pack = ranked
    msg = template_message(pack)
    assert ground_check(msg, pack) == []
    assert msg.recommended_plan_id == plans[0].id and len(msg.headline) <= 90


def test_fact_pack_has_real_names_and_ids(ranked):
    db, _, _, plans, _, pack = ranked
    assert [p["rank"] for p in pack["plans"]] == [1, 2]
    first_change = pack["plans"][0]["changes"][0]
    driver = db.get(Driver, plans[0].actions[0]["driver_id"])
    assert driver.name in first_change and f"driver {driver.id}" in first_change


def test_wrong_recommended_plan_is_caught(ranked):
    *_, pack = ranked
    msg = good_message(pack)
    wrong = with_change(msg, recommended_plan_id=pack["plans"][1]["plan_id"])
    issues = ground_check(wrong, pack)
    assert [i["field"] for i in issues] == ["recommended_plan_id"]


def test_unknown_plan_id_and_missing_plan_are_caught(ranked):
    *_, pack = ranked
    msg = good_message(pack)
    plans = [PlanText(plan_id=424242, rationale="x", tradeoffs="y"), msg.plans[0]]
    issues = ground_check(with_change(msg, plans=[p.model_dump() for p in plans]), pack)
    reasons = {i["reason"] for i in issues}
    assert "not a plan in the fact pack" in reasons and "plan missing from the message" in reasons


def test_made_up_number_is_caught_and_rounding_is_accepted(ranked):
    *_, pack = ranked
    msg = good_message(pack)
    pct = pack["chance_of_hitting_target_pct"]
    fine = with_change(msg, summary=f"There is a {pct}% chance and about {pack['forecast_completions']} completions are forecast.")
    assert ground_check(fine, pack) == []
    score = pack["plans"][0]["score_total"]
    rounded = with_change(msg, summary=f"The best plan scores about {round(score, 1)}.")  # 1-decimal rounding of a fact pack value
    assert ground_check(rounded, pack) == []
    made_up = with_change(msg, summary="We expect 7777 completions.")
    issues = ground_check(made_up, pack)
    assert issues and issues[0]["token"] == "7777" and issues[0]["field"] == "summary"


def test_fake_driver_name_is_caught(ranked):
    db, _, _, _, _, pack = ranked
    msg = good_message(pack)
    invented = with_change(msg, why_at_risk=["Zubair Hartwell keeps missing sessions."])
    assert any(i["token"] == "Zubair Hartwell" for i in ground_check(invented, pack))

    # A real driver who is simply not in the fact pack is caught too.
    in_pack = json.dumps(pack)
    outsider = next(d for d in db.scalars(select(Driver)) if d.name not in in_pack)
    real = with_change(msg, why_at_risk=[f"{outsider.name} is late."])
    assert any(i["token"] == outsider.name for i in ground_check(real, pack, {outsider.name}))


def test_names_from_the_fact_pack_are_fine(ranked):
    db, _, _, plans, _, pack = ranked
    name = db.get(Driver, plans[0].actions[0]["driver_id"]).name
    msg = with_change(good_message(pack), why_at_risk=[f"{name} can take a free seat."])
    assert ground_check(msg, pack, {name}) == []


def run_writer(ranked, script):
    db, alert, run, plans, ctx, _ = ranked
    llm = FakeLLMClient(script)
    write_alert_message(db, llm, alert, run, plans, ctx)
    db.commit()
    return llm, list(db.scalars(select(AlertMessage).where(AlertMessage.alert_id == alert.id).order_by(AlertMessage.id)))


def test_first_attempt_pass_stores_one_row(ranked):
    *_, pack = ranked
    before = len(ranked[0].scalars(select(AlertMessage)).all())
    llm, rows = run_writer(ranked, [text_response(json.dumps(good_message(pack).model_dump()))])
    new = rows[before:]
    assert len(new) == 1 and new[0].grounding_ok and not new[0].used_fallback and new[0].attempts == 1
    assert llm.requests[0]["temperature"] == 0.2
    assert "FACT PACK" in llm.requests[0]["messages"][1]["content"]
    assert ranked[1].message.startswith(good_message(pack).headline)


def test_retry_lists_the_issues_and_then_succeeds(ranked):
    *_, pack = ranked
    bad = with_change(good_message(pack), summary="We expect 7777 completions.")
    before = len(ranked[0].scalars(select(AlertMessage)).all())
    llm, rows = run_writer(ranked, [text_response(json.dumps(bad.model_dump())), text_response(json.dumps(good_message(pack).model_dump()))])
    first, second = rows[before:]
    assert not first.grounding_ok and first.grounding_issues[0]["token"] == "7777" and first.attempts == 1
    assert second.grounding_ok and second.attempts == 2 and not second.used_fallback
    assert "7777" in llm.requests[1]["messages"][1]["content"]  # the issues were sent back


def test_two_failures_use_the_template_which_is_grounded(ranked):
    before = len(ranked[0].scalars(select(AlertMessage)).all())
    _, rows = run_writer(ranked, [text_response("not json at all"), text_response('{"headline": "x"}')])
    first, second, fallback = rows[before:]
    assert not first.grounding_ok and not second.grounding_ok
    assert fallback.used_fallback and fallback.grounding_ok and fallback.model == "template" and fallback.attempts == 2
    shown = AlertMessageOut.model_validate(fallback.parsed)
    assert shown.recommended_plan_id == ranked[3][0].id


def test_audit_row_records_prompt_and_outcome(ranked):
    db = ranked[0]
    run_writer(ranked, [text_response("nope"), text_response("nope")])
    audit = db.scalars(select(AuditLog).where(AuditLog.action == "alert_message_written").order_by(AuditLog.id.desc())).first()
    assert audit.details["used_fallback"] is True and audit.details["prompt_version"] == "alert_writer_v1" and audit.details["prompt_hash"]
    assert len(audit.details["attempt_results"]) == 3
