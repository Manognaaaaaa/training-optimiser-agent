"""Planner API: runs, metrics, the clock guard and startup clean-up. All with a FakeLLMClient."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.database import get_db
from app.llm import get_llm_client
from app.llm.base import LLMError
from app.llm.fake import FakeLLMClient, tool_response
from app.llm.planner import mark_stale_runs_failed
from app.main import app
from app.models import Alert, Plan, PlannerRun
from app.routers.planner import get_session_factory
from tests.test_planner import ALERT_ID, good_plan, submit


@pytest.fixture()
def api(aengine):
    """Returns (client, set_llm). The LLM dependency and the background session factory both use the test database."""
    holder = {"llm": FakeLLMClient([])}
    factory = sessionmaker(bind=aengine, autoflush=False, expire_on_commit=False)

    def override_db():
        with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_llm_client] = lambda: holder["llm"]
    app.dependency_overrides[get_session_factory] = lambda: factory

    def set_llm(llm):
        holder["llm"] = llm

    yield TestClient(app), set_llm, factory
    app.dependency_overrides.clear()


def test_post_returns_202_and_the_run_finishes(api, adb):
    client, set_llm, _ = api
    set_llm(FakeLLMClient([submit(good_plan(adb)), tool_response("submit_no_plan", {"reason": "none"})]))
    resp = client.post("/api/planner/runs", json={"alert_id": ALERT_ID, "n": 2})
    assert resp.status_code == 202
    assert resp.json()["status"] == "queued" and resp.json()["n_requested"] == 2

    detail = client.get(f"/api/planner/runs/{resp.json()['id']}").json()  # background task has finished by now
    assert detail["status"] == "done" and detail["samples_done"] == 2 and detail["n_valid"] == 1 and detail["n_no_plan"] == 1
    first = detail["plans"][0]
    assert first["outcome"] == "valid" and first["strategy"] and first["temperature"] == 0.2
    assert first["action_labels"][0].startswith("Enrol ") and " into " in first["action_labels"][0]
    assert "nationality" not in str(detail).lower()
    assert "trace" not in first and "trace" in client.get(f"/api/planner/runs/{detail['id']}?trace=true").json()["plans"][0]

    listed = client.get(f"/api/planner/runs?alert_id={ALERT_ID}").json()
    assert [r["id"] for r in listed] == [detail["id"]]
    assert client.get("/api/planner/runs?alert_id=2").json() == []


def test_labels_resolve_new_session_refs(api, adb):
    client, set_llm, _ = api
    from app.llm.tools import run_read_only_tool
    from app.services.sim_clock import get_sim_now
    alert = adb.get(Alert, ALERT_ID)
    now = get_sim_now(adb)
    slot = run_read_only_tool(adb, now, "find_trainer_slots", {"course_id": alert.course_id, "limit": 1})["slots"][0]
    eligible = run_read_only_tool(adb, now, "find_eligible_drivers", {"course_id": alert.course_id, "start": slot["start"]})["drivers"][0]
    plan = {"course_id": alert.course_id, "actions": [
        {"type": "add_session", "temp_ref": "new:1", "course_id": alert.course_id, "trainer_id": slot["trainer_id"],
         "start": slot["start"], "end": slot["end"], "location": "Room", "capacity": 5},
        {"type": "enroll_driver", "driver_id": eligible["driver_id"], "session_id": "new:1"},
    ]}
    set_llm(FakeLLMClient([submit(plan)]))
    run_id = client.post("/api/planner/runs", json={"alert_id": ALERT_ID, "n": 1}).json()["id"]
    labels = client.get(f"/api/planner/runs/{run_id}").json()["plans"][0]["action_labels"]
    assert labels[0].startswith("Add session new:1 on ") and "into new session new:1 (" in labels[1]


def test_validation_errors(api, adb):
    client, set_llm, factory = api
    set_llm(FakeLLMClient([]))
    assert client.post("/api/planner/runs", json={"alert_id": 99999}).status_code == 404
    assert client.post("/api/planner/runs", json={"alert_id": ALERT_ID, "n": 0}).status_code == 422
    assert client.post("/api/planner/runs", json={"alert_id": ALERT_ID, "n": 11}).status_code == 422
    assert client.get("/api/planner/runs/99999").status_code == 404
    with factory() as db:
        db.get(Alert, ALERT_ID).status = "resolved"
        db.commit()
    resp = client.post("/api/planner/runs", json={"alert_id": ALERT_ID})
    assert resp.status_code == 409 and "resolved" in resp.json()["detail"]


def test_second_concurrent_run_is_refused_and_clock_is_frozen(api):
    client, set_llm, factory = api
    set_llm(FakeLLMClient([]))
    with factory() as db:
        db.add(PlannerRun(alert_id=ALERT_ID, course_id=1, status="running", n_requested=1))
        db.commit()
    resp = client.post("/api/planner/runs", json={"alert_id": ALERT_ID})
    assert resp.status_code == 409 and "in progress" in resp.json()["detail"]
    assert client.post("/api/sim/advance", json={"days": 1}).status_code == 409
    reset = client.post("/api/sim/reset", json={})
    assert reset.status_code == 409 and reset.json()["detail"] == "A planner run is in progress"


def test_sim_works_again_after_the_run_ends(api):
    client, set_llm, _ = api
    set_llm(FakeLLMClient([tool_response("submit_no_plan", {"reason": "x"})]))
    assert client.post("/api/planner/runs", json={"alert_id": ALERT_ID, "n": 1}).status_code == 202
    assert client.post("/api/sim/advance", json={"days": 1}).status_code == 200


def test_503_when_no_key_is_configured(aengine, monkeypatch):
    factory = sessionmaker(bind=aengine, autoflush=False, expire_on_commit=False)

    def override_db():
        with factory() as session:
            yield session

    monkeypatch.setattr("app.llm.settings.groq_api_key", "")
    app.dependency_overrides[get_db] = override_db
    try:
        resp = TestClient(app).post("/api/planner/runs", json={"alert_id": ALERT_ID})
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 503 and resp.json()["detail"] == "GROQ_API_KEY is not set in backend/.env"


def test_llm_failure_marks_the_run_failed(api):
    client, set_llm, _ = api
    set_llm(FakeLLMClient([LLMError("auth", "bad key")]))
    run_id = client.post("/api/planner/runs", json={"alert_id": ALERT_ID, "n": 2}).json()["id"]
    detail = client.get(f"/api/planner/runs/{run_id}").json()
    assert detail["status"] == "failed" and detail["error"].startswith("auth") and detail["plans"] == []


def test_startup_marks_stale_runs_failed(adb):
    adb.add_all([
        PlannerRun(alert_id=ALERT_ID, course_id=1, status="running", n_requested=1),
        PlannerRun(alert_id=ALERT_ID, course_id=1, status="queued", n_requested=1),
        PlannerRun(alert_id=ALERT_ID, course_id=1, status="done", n_requested=1),
    ])
    adb.commit()
    assert mark_stale_runs_failed(adb) == 2
    statuses = sorted((r.status, r.error) for r in adb.query(PlannerRun))
    assert statuses == [("done", None), ("failed", "server restarted"), ("failed", "server restarted")]


def test_metrics_match_a_hand_computed_case(api, adb):
    client, _, factory = api
    assert client.get("/api/planner/metrics").json()["plans_total"] == 0
    caught = {"code": "UNKNOWN_DRIVER", "category": "hallucination", "entity_type": "driver", "entity_id": 9}
    with factory() as db:
        run = PlannerRun(alert_id=ALERT_ID, course_id=1, status="done", n_requested=4)
        db.add(run)
        db.flush()

        def plan(outcome, steps, tokens, latency, fabricated=None, violations=None):
            db.add(Plan(alert_id=ALERT_ID, run_id=run.id, actions=[], outcome=outcome, n_steps=steps, total_tokens=tokens,
                        latency_ms=latency, fabricated_ids=fabricated or [], rejection_reasons=violations or []))

        plan("valid", 4, 1000, 2000)
        plan("invalid", 6, 2000, 4000, [{"type": "driver", "id": 9}, {"type": "session", "id": 8}], [caught])  # 1 of 2 caught
        plan("schema_invalid", 2, 600, 1000)
        plan("no_plan", 4, 400, 1000)
        db.commit()
    m = client.get("/api/planner/metrics").json()
    assert m["plans_total"] == 4 and m["valid_pct"] == 25.0 and m["schema_invalid_pct"] == 25.0
    assert m["hallucination_pct"] == 25.0 and m["fabricated_ids_total"] == 2 and m["fabricated_ids_caught"] == 1
    assert m["checker_catch_rate"] == 0.5
    assert m["avg_steps"] == 4.0 and m["avg_tokens"] == 1000.0 and m["avg_latency_ms"] == 2000.0
    assert m["outcomes"] == {"valid": 1, "invalid": 1, "schema_invalid": 1, "no_plan": 1}
