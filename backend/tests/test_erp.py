"""Phase 2 tests: CRUD, validation rules, enrollments, calendar, audit trail."""
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import AuditLog, Driver, Enrollment, TrainingSession
from app.services.sessions import fill_band, fill_rate

SUNDAY = "2026-06-07"  # a Sunday in the future of the seeded clock (starts 2026-01-01)
FRIDAY = "2026-06-05"


# --- helpers ---------------------------------------------------------------------------------
def audit_count(engine) -> int:
    with Session(engine) as s:
        return s.scalar(select(func.count()).select_from(AuditLog)) or 0


def last_audit(engine) -> AuditLog:
    with Session(engine) as s:
        return s.scalars(select(AuditLog).order_by(AuditLog.id.desc())).first()


def new_trainer(client, max_per_week=5, name="Test Trainer") -> dict:
    r = client.post("/api/trainers", json={"name": name, "max_sessions_per_week": max_per_week})
    assert r.status_code == 201, r.text
    return r.json()


def course_id(client) -> int:
    return client.get("/api/courses").json()["items"][0]["id"]


def new_session(client, trainer_id: int, when: str = f"{SUNDAY}T09:00:00", **extra):
    body = {"course_id": course_id(client), "trainer_id": trainer_id, "start_time": when, **extra}
    return client.post("/api/sessions", json=body)


# --- generic API behaviour -------------------------------------------------------------------
def test_pages_sorting_and_404(client):
    page = client.get("/api/drivers?page_size=10&sort=-name").json()
    assert set(page) == {"items", "total", "page", "page_size"} and len(page["items"]) == 10 and page["total"] == 300
    names = [d["name"] for d in page["items"]]
    assert names == sorted(names, reverse=True)
    assert client.get("/api/drivers?sort=password").status_code == 422
    assert client.get("/api/drivers?page_size=500").status_code == 422
    for path in ("drivers", "trainers", "courses", "sessions"):
        r = client.get(f"/api/{path}/999999")
        assert r.status_code == 404 and isinstance(r.json()["detail"], str)
    assert client.delete("/api/drivers/999999").status_code == 404
    assert client.post("/api/sessions/999999/cancel").status_code == 404


def test_validation_error_has_string_detail(client):
    r = client.post("/api/drivers", json={"name": ""})
    assert r.status_code == 422 and isinstance(r.json()["detail"], str) and "name" in r.json()["errors"]


# --- drivers ---------------------------------------------------------------------------------
def test_driver_crud_and_audit(client, engine):
    before = audit_count(engine)
    body = {"name": "Test Driver", "nationality": "Pakistani", "shift": "day", "depot": "Al Quoz Depot", "hire_date": "2026-01-01"}
    created = client.post("/api/drivers", json=body)
    assert created.status_code == 201
    d = created.json()
    assert d["employee_code"] == "DRV-0301" and d["is_active"]
    assert audit_count(engine) == before + 1 and last_audit(engine).actor == "user"
    assert last_audit(engine).action == "driver_created"

    assert client.post("/api/drivers", json={**body, "employee_code": "DRV-0001"}).status_code == 409

    found = client.get("/api/drivers?q=test driver").json()
    assert found["total"] == 1 and found["items"][0]["id"] == d["id"]
    assert client.get("/api/drivers?q=drv-0301").json()["total"] == 1
    assert client.get("/api/drivers?shift=night&is_active=true").json()["total"] > 0

    r = client.patch(f"/api/drivers/{d['id']}", json={"depot": "Jebel Ali Depot", "name": "Test Driver"})
    assert r.status_code == 200 and r.json()["depot"] == "Jebel Ali Depot"
    audit = last_audit(engine)
    assert audit.action == "driver_updated" and audit.details == {"depot": ["Al Quoz Depot", "Jebel Ali Depot"]}
    assert audit_count(engine) == before + 2

    assert client.patch(f"/api/drivers/{d['id']}", json={"employee_code": "DRV-0001"}).status_code == 409
    assert client.patch(f"/api/drivers/{d['id']}", json={"is_active": False}).status_code == 409  # use DELETE

    detail = client.get(f"/api/drivers/{d['id']}").json()
    assert detail["history"] == [] and detail["upcoming"] == []
    opts = client.get("/api/drivers/options").json()
    assert "day" in opts["shifts"] and len(opts["depots"]) >= 2

    gone = client.delete(f"/api/drivers/{d['id']}")
    assert gone.status_code == 200 and gone.json()["is_active"] is False and gone.json()["cancelled_bookings"] == 0
    assert last_audit(engine).action == "driver_deactivated" and audit_count(engine) == before + 3


def test_deactivate_cancels_only_future_booked(client, engine):
    client.post("/api/sim/advance", json={"days": 20})
    with Session(engine) as s:
        now = client.get("/api/sim/state").json()["current_time"]
        driver_id = s.scalar(
            select(Enrollment.driver_id).join(TrainingSession, TrainingSession.id == Enrollment.session_id)
            .where(Enrollment.status == "booked", TrainingSession.start_time > now).limit(1)
        )
        # Rows that must stay untouched: everything that is not a booked enrollment.
        untouched = {e.id: e.status for e in s.scalars(select(Enrollment).where(Enrollment.driver_id == driver_id, Enrollment.status != "booked"))}
        future_booked = s.scalar(select(func.count()).select_from(Enrollment).where(Enrollment.driver_id == driver_id, Enrollment.status == "booked"))
    assert future_booked > 0 and untouched  # driver has past history as well

    r = client.delete(f"/api/drivers/{driver_id}").json()
    assert r["cancelled_bookings"] == future_booked
    with Session(engine) as s:
        rows = {e.id: e.status for e in s.scalars(select(Enrollment).where(Enrollment.driver_id == driver_id))}
    assert all(rows[i] == status for i, status in untouched.items())
    assert "booked" not in rows.values()
    assert last_audit(engine).details == {"cancelled_bookings": future_booked}


def test_unavailability_cancels_overlapping_booking(client, engine):
    with Session(engine) as s:
        enrollment = s.scalars(select(Enrollment).where(Enrollment.status == "booked")).first()
        session = s.get(TrainingSession, enrollment.session_id)
        driver_id, enrollment_id = enrollment.driver_id, enrollment.id
        start, end = session.start_time.isoformat(), session.end_time.isoformat()
    before = audit_count(engine)
    r = client.post(f"/api/drivers/{driver_id}/unavailability", json={"start_time": start, "end_time": end, "reason": "annual leave"})
    assert r.status_code == 201 and r.json()["cancelled_bookings"] >= 1
    assert audit_count(engine) == before + 1
    with Session(engine) as s:
        assert s.get(Enrollment, enrollment_id).status == "cancelled"
    listed = client.get(f"/api/drivers/{driver_id}/unavailability").json()
    assert any(u["id"] == r.json()["id"] for u in listed)
    assert client.post(f"/api/drivers/{driver_id}/unavailability", json={"start_time": end, "end_time": start, "reason": "x"}).status_code == 422
    assert client.delete(f"/api/drivers/{driver_id}/unavailability/{r.json()['id']}").status_code == 204
    assert client.delete(f"/api/drivers/{driver_id}/unavailability/{r.json()['id']}").status_code == 404
    assert audit_count(engine) == before + 2


# --- trainers --------------------------------------------------------------------------------
def test_trainer_crud_and_delete_rules(client, engine):
    before = audit_count(engine)
    t = new_trainer(client)
    assert t["sessions_this_week"] == 0 and audit_count(engine) == before + 1
    assert client.get("/api/trainers?q=test trainer").json()["total"] == 1

    r = client.patch(f"/api/trainers/{t['id']}", json={"max_sessions_per_week": 3})
    assert r.status_code == 200 and r.json()["max_sessions_per_week"] == 3
    assert last_audit(engine).details == {"max_sessions_per_week": [5, 3]}

    assert client.delete(f"/api/trainers/{t['id']}").status_code == 204
    assert client.get(f"/api/trainers/{t['id']}").status_code == 404

    busy = client.get("/api/trainers").json()["items"][0]
    r = client.delete(f"/api/trainers/{busy['id']}")
    assert r.status_code == 409 and "Reassign" in r.json()["detail"]


def test_trainer_list_shows_week_load_and_limit_warning(client):
    items = client.get("/api/trainers?page_size=200").json()["items"]
    assert all("sessions_this_week" in t for t in items) and any(t["sessions_this_week"] > 0 for t in items)
    r = client.patch("/api/trainers/1", json={"max_sessions_per_week": 1})  # trainer 1 is the busiest (41 sessions)
    assert r.status_code == 200 and r.json()["warnings"] and "Week starting" in r.json()["warnings"][0]


# --- courses ---------------------------------------------------------------------------------
def test_course_crud_with_target(client, engine):
    before = audit_count(engine)
    body = {"code": "TST-1", "name": "Test Course", "duration_hours": 2, "default_capacity": 10, "target_completions": 120}
    r = client.post("/api/courses", json=body)
    assert r.status_code == 201 and r.json()["target_completions"] == 120
    cid = r.json()["id"]
    assert client.post("/api/courses", json=body).status_code == 409
    assert audit_count(engine) == before + 1

    r = client.patch(f"/api/courses/{cid}", json={"target_completions": 150, "name": "Test Course"})
    assert r.json()["target_completions"] == 150
    assert last_audit(engine).details == {"target_completions": [120, 150]}
    assert client.patch(f"/api/courses/{cid}", json={"code": client.get("/api/courses").json()["items"][0]["code"]}).status_code == 409

    listed = client.get("/api/courses?q=TST").json()["items"]
    assert listed[0]["target_completions"] == 150
    assert all(c["target_completions"] is not None for c in client.get("/api/courses").json()["items"])

    assert client.delete(f"/api/courses/{cid}").status_code == 204
    assert client.get(f"/api/courses/{cid}").status_code == 404

    used = client.get("/api/courses").json()["items"][0]
    assert client.delete(f"/api/courses/{used['id']}").status_code == 409


# --- sessions --------------------------------------------------------------------------------
def test_session_create_update_cancel(client, engine):
    t = new_trainer(client)
    before = audit_count(engine)
    r = new_session(client, t["id"], capacity=6)
    assert r.status_code == 201
    s = r.json()
    assert s["source"] == "manual" and s["status"] == "scheduled" and s["capacity"] == 6 and s["is_editable"]
    assert s["end_time"] > s["start_time"] and s["booked"] == 0
    assert audit_count(engine) == before + 1 and last_audit(engine).actor == "user"

    default_cap = new_session(client, t["id"], f"{SUNDAY}T14:00:00").json()["capacity"]
    assert default_cap == client.get("/api/courses").json()["items"][0]["default_capacity"]

    got = client.get("/api/sessions?trainer_id=%d&sort=start_time" % t["id"]).json()
    assert got["total"] == 2 and got["items"][0]["id"] == s["id"]
    assert client.get(f"/api/sessions?trainer_id={t['id']}&status=cancelled").json()["total"] == 0

    r = client.patch(f"/api/sessions/{s['id']}", json={"capacity": 8, "location": s["location"]})
    assert r.status_code == 200 and r.json()["capacity"] == 8
    assert last_audit(engine).details == {"capacity": [6, 8]}

    assert client.patch(f"/api/sessions/{s['id']}", json={"start_time": f"{FRIDAY}T09:00:00"}).status_code == 422
    # moving onto the trainer's other session is a double booking
    assert client.patch(f"/api/sessions/{s['id']}", json={"start_time": f"{SUNDAY}T14:00:00"}).status_code == 409

    # cancel releases the bookings
    driver_id = client.get(f"/api/sessions/{s['id']}/eligible-drivers").json()[0]["id"]
    assert client.post(f"/api/sessions/{s['id']}/enrollments", json={"driver_id": driver_id}).status_code == 201
    before = audit_count(engine)
    c = client.post(f"/api/sessions/{s['id']}/cancel")
    assert c.status_code == 200 and c.json()["status"] == "cancelled" and c.json()["fill_band"] == "cancelled"
    assert last_audit(engine).details == {"cancelled_bookings": 1} and audit_count(engine) == before + 1
    assert client.get(f"/api/sessions/{s['id']}/enrollments").json()[0]["status"] == "cancelled"
    assert client.post(f"/api/sessions/{s['id']}/cancel").status_code == 409
    assert client.patch(f"/api/sessions/{s['id']}", json={"capacity": 5}).status_code == 409


def test_session_create_rejections(client):
    t = new_trainer(client, max_per_week=2)
    now = client.get("/api/sim/state").json()["current_time"]
    assert now.startswith("2026-01-01")
    past = new_session(client, t["id"], "2025-12-28T09:00:00")
    assert past.status_code == 422 and "future" in past.json()["errors"]["start_time"]
    fri = new_session(client, t["id"], f"{FRIDAY}T09:00:00")
    assert fri.status_code == 422 and "Friday" in fri.json()["detail"]
    eleven = new_session(client, t["id"], f"{SUNDAY}T11:00:00")
    assert eleven.status_code == 422 and "09:00" in eleven.json()["detail"]
    assert new_session(client, t["id"], capacity=0).status_code == 422
    assert new_session(client, t["id"], capacity=31).status_code == 422
    assert client.post("/api/sessions", json={"course_id": 9999, "trainer_id": t["id"], "start_time": f"{SUNDAY}T09:00:00"}).status_code == 422
    assert client.post("/api/sessions", json={"course_id": 1, "trainer_id": t["id"], "start_time": "2026-06-07T09:00:00Z"}).status_code == 422

    assert new_session(client, t["id"]).status_code == 201
    other_course = client.get("/api/courses").json()["items"][1]["id"]
    clash = client.post("/api/sessions", json={"course_id": other_course, "trainer_id": t["id"], "start_time": f"{SUNDAY}T09:00:00"})
    assert clash.status_code == 409 and "double booked" in clash.json()["detail"]

    assert new_session(client, t["id"], "2026-06-08T09:00:00").status_code == 201  # 2 of 2 this week
    over = new_session(client, t["id"], "2026-06-09T09:00:00")
    assert over.status_code == 409 and "2 of 2" in over.json()["detail"]
    assert new_session(client, t["id"], "2026-06-14T09:00:00").status_code == 201  # next week is fine


def test_completed_sessions_are_read_only(client):
    client.post("/api/sim/advance", json={"days": 30})
    done = client.get("/api/sessions?status=completed&page_size=1").json()["items"][0]
    assert done["is_editable"] is False
    assert client.patch(f"/api/sessions/{done['id']}", json={"capacity": 5}).status_code == 409
    assert client.post(f"/api/sessions/{done['id']}/cancel").status_code == 409
    assert client.post(f"/api/sessions/{done['id']}/enrollments", json={"driver_id": 1}).status_code == 409


def test_lowering_capacity_below_booked_is_409(client):
    t = new_trainer(client)
    s = new_session(client, t["id"], capacity=5).json()
    for d in client.get(f"/api/sessions/{s['id']}/eligible-drivers").json()[:2]:
        assert client.post(f"/api/sessions/{s['id']}/enrollments", json={"driver_id": d["id"]}).status_code == 201
    r = client.patch(f"/api/sessions/{s['id']}", json={"capacity": 1})
    assert r.status_code == 409 and "2 drivers" in r.json()["detail"]


# --- enrollments -----------------------------------------------------------------------------
def test_enrollment_full_and_remove_and_readd(client, engine):
    t = new_trainer(client)
    s = new_session(client, t["id"], capacity=1).json()
    a, b = client.get(f"/api/sessions/{s['id']}/eligible-drivers").json()[:2]
    before = audit_count(engine)
    first = client.post(f"/api/sessions/{s['id']}/enrollments", json={"driver_id": a["id"]})
    assert first.status_code == 201 and first.json()["status"] == "booked" and audit_count(engine) == before + 1
    full = client.post(f"/api/sessions/{s['id']}/enrollments", json={"driver_id": b["id"]})
    assert full.status_code == 409 and "full" in full.json()["detail"]
    assert client.get(f"/api/sessions/{s['id']}/eligible-drivers").json() == []
    assert client.post(f"/api/sessions/{s['id']}/enrollments", json={"driver_id": a["id"]}).status_code == 409

    removed = client.delete(f"/api/sessions/{s['id']}/enrollments/{first.json()['id']}")
    assert removed.status_code == 200 and removed.json()["status"] == "cancelled"
    assert client.delete(f"/api/sessions/{s['id']}/enrollments/{first.json()['id']}").status_code == 409
    again = client.post(f"/api/sessions/{s['id']}/enrollments", json={"driver_id": a["id"]})  # revives the old row
    assert again.status_code == 201 and again.json()["id"] == first.json()["id"]
    assert client.post(f"/api/sessions/{s['id']}/enrollments", json={"driver_id": 99999}).status_code == 404


def test_enrollment_rejects_driver_who_already_attended(client, engine):
    client.post("/api/sim/advance", json={"days": 30})
    now = client.get("/api/sim/state").json()["current_time"]
    with Session(engine) as s:
        driver_id, course = s.execute(
            select(Enrollment.driver_id, TrainingSession.course_id)
            .join(TrainingSession, TrainingSession.id == Enrollment.session_id)
            .where(Enrollment.status == "attended").limit(1)
        ).one()
    t = new_trainer(client)
    new = client.post("/api/sessions", json={"course_id": course, "trainer_id": t["id"], "start_time": f"{SUNDAY}T09:00:00"}).json()
    r = client.post(f"/api/sessions/{new['id']}/enrollments", json={"driver_id": driver_id})
    assert r.status_code == 409 and "already attended" in r.json()["detail"]
    assert all(d["id"] != driver_id for d in client.get(f"/api/sessions/{new['id']}/eligible-drivers?q=").json())
    assert now


def test_enrollment_rejected_when_driver_not_free(client):
    t = new_trainer(client)
    s = new_session(client, t["id"], capacity=10).json()
    driver = client.get(f"/api/sessions/{s['id']}/eligible-drivers").json()[0]
    client.post(f"/api/drivers/{driver['id']}/unavailability", json={
        "start_time": f"{SUNDAY}T00:00:00", "end_time": "2026-06-08T00:00:00", "reason": "annual leave"})
    r = client.post(f"/api/sessions/{s['id']}/enrollments", json={"driver_id": driver["id"]})
    assert r.status_code == 409 and "unavailable: annual leave" in r.json()["detail"]
    assert all(d["id"] != driver["id"] for d in client.get(f"/api/sessions/{s['id']}/eligible-drivers").json())


def test_booked_elsewhere_and_inactive_rejected(client):
    t1, t2 = new_trainer(client, name="T1"), new_trainer(client, name="T2")
    first = new_session(client, t1["id"], capacity=10).json()
    other_course = client.get("/api/courses").json()["items"][1]["id"]
    second = client.post("/api/sessions", json={"course_id": other_course, "trainer_id": t2["id"], "start_time": f"{SUNDAY}T09:00:00"}).json()
    driver = client.get(f"/api/sessions/{first['id']}/eligible-drivers").json()[0]
    assert client.post(f"/api/sessions/{first['id']}/enrollments", json={"driver_id": driver["id"]}).status_code == 201
    r = client.post(f"/api/sessions/{second['id']}/enrollments", json={"driver_id": driver["id"]})
    assert r.status_code == 409 and "same time" in r.json()["detail"]
    other = client.get(f"/api/sessions/{second['id']}/eligible-drivers").json()[0]["id"]
    client.delete(f"/api/drivers/{other}")
    r = client.post(f"/api/sessions/{second['id']}/enrollments", json={"driver_id": other})
    assert r.status_code == 409 and "inactive" in r.json()["detail"]


def test_eligible_list_agrees_with_post(client):
    """For 40 drivers: listed as eligible exactly when POST accepts them."""
    t = new_trainer(client)
    s = new_session(client, t["id"], capacity=30).json()
    codes = [d["employee_code"] for d in client.get("/api/drivers?page_size=40").json()["items"]]
    ids = {d["employee_code"]: d["id"] for d in client.get("/api/drivers?page_size=40").json()["items"]}
    accepted = rejected = 0
    for code in codes:
        eligible = client.get(f"/api/sessions/{s['id']}/eligible-drivers?q={code}").json()
        r = client.post(f"/api/sessions/{s['id']}/enrollments", json={"driver_id": ids[code]})
        if eligible:
            assert r.status_code == 201, (code, r.text)
            accepted += 1
        else:
            assert r.status_code == 409, (code, r.text)
            rejected += 1
    assert accepted > 0 and rejected > 0  # the sample exercises both outcomes


# --- calendar and fill rate ------------------------------------------------------------------
def test_fill_rate_and_band_rules():
    assert fill_rate("scheduled", 10, 4, 0) == 0.4 and fill_rate("completed", 10, 9, 8) == 0.8
    assert fill_rate("cancelled", 10, 5, 5) is None
    assert [fill_band("scheduled", r) for r in (0.0, 0.49, 0.5, 0.79, 0.8, 1.0)] == ["low", "low", "medium", "medium", "high", "high"]
    assert fill_band("cancelled", None) == "cancelled"


def test_calendar_scheduled_session_fill(client):
    t = new_trainer(client)
    s = new_session(client, t["id"], capacity=4).json()
    window = "start=2026-06-01T00:00:00&end=2026-06-30T00:00:00"

    def event():
        events = client.get(f"/api/calendar?{window}&trainer_id={t['id']}").json()["events"]
        return events[0]

    assert event()["fill_rate"] == 0 and event()["fill_band"] == "low"
    drivers = client.get(f"/api/sessions/{s['id']}/eligible-drivers").json()
    for d in drivers[:2]:
        client.post(f"/api/sessions/{s['id']}/enrollments", json={"driver_id": d["id"]})
    assert event()["fill_rate"] == 0.5 and event()["fill_band"] == "medium" and event()["booked"] == 2
    for d in drivers[2:4]:
        client.post(f"/api/sessions/{s['id']}/enrollments", json={"driver_id": d["id"]})
    assert event()["fill_rate"] == 1.0 and event()["fill_band"] == "high"
    body = client.get(f"/api/calendar?{window}&trainer_id={t['id']}").json()
    assert body["sim_now"].startswith("2026-01-01")

    client.post(f"/api/sessions/{s['id']}/cancel")
    assert client.get(f"/api/calendar?{window}&trainer_id={t['id']}").json()["events"] == []
    shown = client.get(f"/api/calendar?{window}&trainer_id={t['id']}&include_cancelled=true").json()["events"]
    assert shown[0]["fill_band"] == "cancelled" and shown[0]["fill_rate"] is None


def test_calendar_completed_session_uses_attended(client):
    client.post("/api/sim/advance", json={"days": 30})
    events = client.get("/api/calendar?start=2026-01-01T00:00:00&end=2026-01-31T00:00:00").json()["events"]
    done = [e for e in events if e["status"] == "completed"]
    assert done
    for e in done:
        assert e["fill_rate"] == pytest.approx(e["attended"] / e["capacity"], abs=0.001)
    assert any(e["booked"] != e["attended"] for e in done)  # no-shows exist, so it really is attended/capacity


def test_calendar_range_limit(client):
    assert client.get("/api/calendar?start=2026-01-01T00:00:00&end=2026-03-10T00:00:00").status_code == 422
    assert client.get("/api/calendar?start=2026-01-01T00:00:00&end=2026-03-03T00:00:00").status_code == 200  # 61 days
    assert client.get("/api/calendar?start=2026-02-01T00:00:00&end=2026-01-01T00:00:00").status_code == 422
    assert client.get("/api/calendar").status_code == 422


def test_calendar_query_count_does_not_grow_with_sessions(client, engine):
    """One joined query, not one per session."""
    from sqlalchemy import event

    statements: list[str] = []
    event.listen(engine, "before_cursor_execute", lambda conn, cur, stmt, *a: statements.append(stmt))
    client.get("/api/calendar?start=2026-02-01T00:00:00&end=2026-03-01T00:00:00")
    assert len(statements) <= 3  # sim clock + sessions
