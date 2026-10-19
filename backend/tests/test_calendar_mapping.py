import re

from sqlalchemy import select

from app.calendar.base import APP_TAG
from app.calendar.mapping import build_event, content_hash, event_id_for
from app.models import Alert, CalendarOutbox, Driver, Enrollment, Plan, TrainingSession


def a_session_with_roster(db):
    session = db.scalars(select(TrainingSession).where(TrainingSession.status == "scheduled")).first()
    if not db.scalars(select(Enrollment).where(Enrollment.session_id == session.id, Enrollment.status == "booked")).first():
        driver = db.scalars(select(Driver)).first()
        db.add(Enrollment(session_id=session.id, driver_id=driver.id, status="booked"))
        db.flush()
    return session


def test_event_id_is_deterministic_and_valid_for_google():
    a, b = event_id_for(7, "dev"), event_id_for(7, "dev")
    assert a == b and re.fullmatch(r"[a-v0-9]{5,40}", a) and a.startswith("tos")
    assert event_id_for(7, "dev") != event_id_for(8, "dev")


def test_different_env_gives_a_different_id():
    assert event_id_for(7, "dev") != event_id_for(7, "alice")


def test_event_has_timezone_roster_and_tags(db):
    session = a_session_with_roster(db)
    body = build_event(session, db)
    assert body["start"]["timeZone"] == "Asia/Dubai" and body["end"]["timeZone"] == "Asia/Dubai"
    first = db.scalar(select(Driver).join(Enrollment, Enrollment.driver_id == Driver.id).where(Enrollment.session_id == session.id))
    assert first.name in body["description"]
    private = body["extendedProperties"]["private"]
    assert private["app"] == APP_TAG and private["session_id"] == str(session.id) and "attendees" not in body
    assert body["colorId"] in {"10", "5", "11"}


def test_plan_and_alert_ids_appear_for_plan_sessions(adb):
    alert = adb.scalars(select(Alert)).first()
    plan = Plan(alert_id=alert.id, actions=[], rank=1, rationale="Closes the gap with one extra session")
    adb.add(plan)
    adb.flush()
    session = adb.scalars(select(TrainingSession).where(TrainingSession.status == "scheduled")).first()
    session.source = "agent"
    adb.add(CalendarOutbox(plan_id=plan.id, alert_id=plan.alert_id, session_id=session.id, change_type="create_session",
                           source="plan_approval", payload={}))
    adb.flush()
    body = build_event(session, adb)
    assert "(added by plan)" in body["summary"]
    assert f"Plan {plan.id} for alert {plan.alert_id}" in body["description"]
    assert f"/alerts/{plan.alert_id}" in body["description"]
    assert body["extendedProperties"]["private"]["plan_id"] == str(plan.id)


def test_hash_is_stable_and_changes_with_the_roster(db):
    session = a_session_with_roster(db)
    first = build_event(session, db)
    assert build_event(session, db)["extendedProperties"]["private"]["content_hash"] == first["extendedProperties"]["private"]["content_hash"]
    assert content_hash(first) == first["extendedProperties"]["private"]["content_hash"]
    extra = db.scalars(select(Driver).where(~Driver.id.in_(select(Enrollment.driver_id).where(Enrollment.session_id == session.id)))).first()
    db.add(Enrollment(session_id=session.id, driver_id=extra.id, status="booked"))
    db.flush()
    assert build_event(session, db)["extendedProperties"]["private"]["content_hash"] != first["extendedProperties"]["private"]["content_hash"]
