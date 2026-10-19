"""Phase 6: approving and rejecting plans. No network (FakeLLMClient)."""
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.llm.fake import FakeLLMClient
from app.llm.planner import run_planner
from app.models import Alert, AlertDecision, AuditLog, CalendarOutbox, Enrollment, Plan, Trainer, TrainingSession
from app.services import decisions
from app.services.errors import Conflict, Invalid
from app.services.sim_clock import get_sim_now, set_sim_now
from tests.test_planner import ALERT_ID, good_plan, submit

TRAINER = 1


def make_run(adb, sizes=(2,)):
    plans = [good_plan(adb, how_many=n) for n in sizes]
    run = run_planner(adb, ALERT_ID, len(plans), FakeLLMClient([submit(p) for p in plans]))
    ranked = sorted(adb.scalars(select(Plan).where(Plan.run_id == run.id, Plan.rank.is_not(None))), key=lambda p: p.rank)
    return plans, ranked


def count(db, model, *where):
    return db.scalar(select(func.count()).select_from(model).where(*where))


def snapshot(db):
    return sorted((e.session_id, e.driver_id, e.status) for e in db.scalars(select(Enrollment)))


def test_approve_applies_the_plan_and_queues_outbox_rows(adb):
    plans, ranked = make_run(adb)
    plan = ranked[0]
    before = count(adb, Enrollment)
    result = decisions.approve_plan(adb, plan.id, TRAINER)

    assert result["status"] == "approved" and result["already_approved"] is False
    assert count(adb, Enrollment) == before + 2
    for a in plans[0]["actions"]:
        row = adb.scalar(select(Enrollment).where(Enrollment.session_id == a["session_id"], Enrollment.driver_id == a["driver_id"]))
        assert row is not None and row.status == "booked"
    outbox = list(adb.scalars(select(CalendarOutbox).where(CalendarOutbox.plan_id == plan.id)))
    assert len(outbox) == 2 and {o.change_type for o in outbox} == {"enroll_driver"} and all(o.status == "pending" for o in outbox)

    assert adb.get(Alert, ALERT_ID).status == "approved" and adb.get(Plan, plan.id).status == "approved"
    decision = adb.scalars(select(AlertDecision)).one()
    assert decision.decision == "approve" and decision.plan_id == plan.id and decision.actor_trainer_id == TRAINER
    assert adb.scalars(select(AuditLog).where(AuditLog.action == "plan_approved", AuditLog.entity_id == plan.id)).one().actor == "user"


def test_approve_supersedes_the_other_plans(adb):
    _, ranked = make_run(adb, sizes=(1, 2, 3))
    decisions.approve_plan(adb, ranked[0].id, TRAINER)
    assert [adb.get(Plan, p.id).status for p in ranked] == ["approved", "superseded", "superseded"]


def test_approve_twice_is_idempotent(adb):
    _, ranked = make_run(adb)
    first = decisions.approve_plan(adb, ranked[0].id, TRAINER)
    state = snapshot(adb)
    outbox = count(adb, CalendarOutbox)
    second = decisions.approve_plan(adb, ranked[0].id, TRAINER)
    assert second["already_approved"] is True and second["outbox"] == first["outbox"]
    assert snapshot(adb) == state and count(adb, CalendarOutbox) == outbox and count(adb, AlertDecision) == 1


def test_approve_creates_new_sessions_and_move_cancels_the_old_booking(adb):
    from app.llm.tools import run_read_only_tool
    alert = adb.get(Alert, ALERT_ID)
    now = get_sim_now(adb)
    slot = run_read_only_tool(adb, now, "find_trainer_slots", {"course_id": alert.course_id, "limit": 1})["slots"][0]
    eligible = run_read_only_tool(adb, now, "find_eligible_drivers", {"course_id": alert.course_id, "start": slot["start"]})["drivers"]
    plan = {"course_id": alert.course_id, "actions": [
        {"type": "add_session", "temp_ref": "new:1", "course_id": alert.course_id, "trainer_id": slot["trainer_id"],
         "start": slot["start"], "end": slot["end"], "location": "Room 9", "capacity": 5},
        {"type": "enroll_driver", "driver_id": eligible[0]["driver_id"], "session_id": "new:1"},
    ]}
    run_planner(adb, ALERT_ID, 1, FakeLLMClient([submit(plan)]))
    ranked = list(adb.scalars(select(Plan).where(Plan.rank == 1)))
    result = decisions.approve_plan(adb, ranked[0].id, TRAINER)

    session = adb.scalars(select(TrainingSession).where(TrainingSession.source == "agent")).one()
    assert session.location == "Room 9" and session.capacity == 5 and session.status == "scheduled"
    booking = adb.scalar(select(Enrollment).where(Enrollment.session_id == session.id))
    assert booking.driver_id == eligible[0]["driver_id"] and booking.status == "booked"
    assert [o["change_type"] for o in result["outbox"]] == ["create_session", "enroll_driver"]
    assert result["outbox"][1]["payload"]["session_id"] == session.id  # the temp ref was replaced by the real id


def test_stale_plan_returns_409_and_changes_nothing(adb):
    plans, ranked = make_run(adb)
    session_id = plans[0]["actions"][0]["session_id"]
    adb.get(TrainingSession, session_id).capacity = 0  # the session filled up since the plan was made
    adb.commit()
    state = snapshot(adb)
    with pytest.raises(Conflict) as err:
        decisions.approve_plan(adb, ranked[0].id, TRAINER)
    assert err.value.extra["reason"] == "stale" and err.value.extra["violations"]
    assert snapshot(adb) == state and count(adb, CalendarOutbox) == 0
    assert adb.get(Plan, ranked[0].id).status == "stale" and adb.get(Alert, ALERT_ID).status == "plans_ready"
    assert adb.scalars(select(AuditLog).where(AuditLog.action == "plan_stale")).one().details["violation_codes"]


def test_expired_plan_returns_409(adb):
    plans, ranked = make_run(adb)
    start = adb.get(TrainingSession, plans[0]["actions"][0]["session_id"]).start_time
    set_sim_now(adb, start + timedelta(hours=1))
    adb.commit()
    state = snapshot(adb)
    with pytest.raises(Conflict, match="expired"):
        decisions.approve_plan(adb, ranked[0].id, TRAINER)
    assert adb.get(Plan, ranked[0].id).status == "expired" and snapshot(adb) == state


def test_failure_in_the_middle_rolls_everything_back(adb, monkeypatch):
    _, ranked = make_run(adb)
    state = snapshot(adb)
    real_book = decisions._book
    calls = []

    def flaky(db, session_id, driver_id):
        calls.append(driver_id)
        if len(calls) == 2:
            raise RuntimeError("disk on fire")
        real_book(db, session_id, driver_id)

    monkeypatch.setattr(decisions, "_book", flaky)
    with pytest.raises(RuntimeError):
        decisions.approve_plan(adb, ranked[0].id, TRAINER)
    assert len(calls) == 2 and snapshot(adb) == state and count(adb, CalendarOutbox) == 0 and count(adb, AlertDecision) == 0
    assert adb.get(Plan, ranked[0].id).status == "valid" and adb.get(Alert, ALERT_ID).status == "plans_ready"


def test_cannot_approve_an_invalid_plan_or_an_unknown_actor(adb):
    _, ranked = make_run(adb)
    with pytest.raises(Exception, match="Trainer 99999 not found"):
        decisions.approve_plan(adb, ranked[0].id, 99999)
    invalid = Plan(alert_id=ALERT_ID, actions=[], status="invalid")
    adb.add(invalid)
    adb.commit()
    with pytest.raises(Conflict, match="invalid"):
        decisions.approve_plan(adb, invalid.id, TRAINER)


# --- Reject --------------------------------------------------------------------------------------------
def test_reject_requires_a_known_reason_and_text_for_other(adb):
    _, ranked = make_run(adb)
    with pytest.raises(Invalid):
        decisions.reject_plan(adb, ranked[0].id, TRAINER, None, None)
    with pytest.raises(Invalid):
        decisions.reject_plan(adb, ranked[0].id, TRAINER, "because", None)
    with pytest.raises(Invalid, match="explain"):
        decisions.reject_plan(adb, ranked[0].id, TRAINER, "other", "  ")
    assert adb.get(Plan, ranked[0].id).status == "valid"
    decisions.reject_plan(adb, ranked[0].id, TRAINER, "other", "Trainer is on leave")
    decision = adb.scalars(select(AlertDecision)).one()
    assert decision.decision == "reject_plan" and decision.reason_code == "other" and decision.reason_text == "Trainer is on leave"


def test_rejecting_the_last_presented_plan_closes_the_alert(adb):
    _, ranked = make_run(adb, sizes=(1, 2))
    first = decisions.reject_plan(adb, ranked[0].id, TRAINER, "unfair", None)
    assert first["alert_status"] == "plans_ready"
    second = decisions.reject_plan(adb, ranked[1].id, TRAINER, "too_disruptive", None)
    assert second["alert_status"] == "rejected" and adb.get(Alert, ALERT_ID).status == "rejected"
    with pytest.raises(Conflict):
        decisions.approve_plan(adb, ranked[0].id, TRAINER)


def test_reject_whole_alert_and_dismiss(adb):
    _, ranked = make_run(adb, sizes=(1, 2))
    decisions.reject_alert(adb, ALERT_ID, TRAINER, "not_feasible", None)
    assert adb.get(Alert, ALERT_ID).status == "rejected"
    assert {adb.get(Plan, p.id).status for p in ranked} == {"rejected"}
    assert adb.scalars(select(AlertDecision)).one().decision == "reject_alert"

    other = adb.scalars(select(Alert).where(Alert.status == "open").order_by(Alert.id)).first()
    decisions.dismiss_alert(adb, other.id, TRAINER, "not worth it")
    assert adb.get(Alert, other.id).status == "dismissed"
    assert adb.scalars(select(AlertDecision).where(AlertDecision.alert_id == other.id)).one().decision == "dismiss"
    assert count(adb, Trainer) > 0
