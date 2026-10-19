"""Phase 6 API: alert list filters, detail payload, generate / approve / reject over HTTP, and alert sync rules."""
from datetime import datetime

from sqlalchemy import select

from app.llm.fake import FakeLLMClient, tool_response
from app.models import Alert, Plan
from app.services.alerts import sync_alerts
from app.services.tracking import load_facts
from app.services.tracking_jobs import compute_forecasts
from tests.test_planner import ALERT_ID, good_plan, submit
from tests.test_planner_api import api  # noqa: F401  (the fixture: client, set_llm, factory)


def generate(client, set_llm, adb, sizes=(1, 2, 3), extra=()):
    set_llm(FakeLLMClient([submit(good_plan(adb, how_many=n)) for n in sizes] + list(extra)))
    resp = client.post(f"/api/alerts/{ALERT_ID}/generate", json={"n": len(sizes)})
    assert resp.status_code == 202
    return resp


def test_list_filters_sorting_and_counts(api, adb):
    client, *_ = api
    rows = client.get("/api/alerts").json()
    assert len(rows) >= 2 and {"gap", "course_name", "status", "risk_level"} <= set(rows[0])
    ranks = [{"high": 2, "medium": 1}.get(r["risk_level"], 0) for r in rows]
    assert ranks == sorted(ranks, reverse=True)  # high risk first

    assert all(r["status"] == "open" for r in client.get("/api/alerts?status=open").json())
    assert client.get("/api/alerts?status=plans_ready").json() == []
    highs = client.get("/api/alerts?risk_level=high").json()
    assert highs and all(r["risk_level"] == "high" for r in highs)
    assert [r["id"] for r in client.get(f"/api/alerts?course_id={rows[0]['course_id']}").json()] == [
        r["id"] for r in rows if r["course_id"] == rows[0]["course_id"]]
    assert client.get("/api/alerts/counts").json()["plans_ready"] == 0


def test_generate_then_detail_has_ranked_plans_and_a_message(api, adb):
    client, set_llm, _ = api
    generate(client, set_llm, adb)  # the alert writer finds an empty script and falls back to the template

    detail = client.get(f"/api/alerts/{ALERT_ID}").json()
    assert detail["alert"]["status"] == "plans_ready" and detail["top_k"] == 3
    assert [p["rank"] for p in detail["presented"]] == [1, 2, 3] and detail["other_valid"] == [] and detail["invalid"] == []
    first = detail["presented"][0]
    assert set(first["score_breakdown"]["components"]) == {"closure", "confidence", "fairness", "disruption", "resource"}
    assert first["changes"][0]["type"] == "enroll_driver" and first["changes"][0]["driver_name"]
    assert detail["message"]["used_fallback"] is True and detail["message"]["grounding_ok"] is True
    assert detail["message"]["message"]["recommended_plan_id"] == first["id"]
    assert detail["run"]["status"] == "done" and detail["decisions"] == []
    assert detail["forecast"]["target"] and detail["forecast"]["p_hit"] is not None
    assert client.get("/api/alerts/counts").json()["plans_ready"] == 1


def test_top_k_collapses_the_rest(api, adb, monkeypatch):
    client, set_llm, _ = api
    monkeypatch.setattr("app.config.settings.top_k", 2)
    generate(client, set_llm, adb)
    detail = client.get(f"/api/alerts/{ALERT_ID}").json()
    assert len(detail["presented"]) == 2 and len(detail["other_valid"]) == 1 and detail["other_valid"][0]["rank"] == 3


def test_invalid_plans_are_listed_separately(api, adb):
    client, set_llm, _ = api
    bad = good_plan(adb, how_many=1)
    bad["actions"][0]["driver_id"] = 999999
    set_llm(FakeLLMClient([submit(good_plan(adb, how_many=2)), submit(bad), tool_response("submit_no_plan", {"reason": "x"})]))
    client.post(f"/api/alerts/{ALERT_ID}/generate", json={"n": 3})
    detail = client.get(f"/api/alerts/{ALERT_ID}").json()
    assert len(detail["presented"]) == 1
    assert [p["violations"][0]["code"] for p in detail["invalid"] if p["violations"]] == ["UNKNOWN_DRIVER"]


def test_generate_rules_and_regenerate_supersedes_old_plans(api, adb):
    client, set_llm, factory = api
    generate(client, set_llm, adb, sizes=(1,))
    old = client.get(f"/api/alerts/{ALERT_ID}").json()["presented"][0]["id"]

    set_llm(FakeLLMClient([submit(good_plan(adb, how_many=2))]))
    assert client.post(f"/api/alerts/{ALERT_ID}/regenerate", json={"n": 1}).status_code == 202
    detail = client.get(f"/api/alerts/{ALERT_ID}").json()
    assert detail["alert"]["status"] == "plans_ready" and len(detail["presented"]) == 1 and detail["presented"][0]["id"] != old
    with factory() as db:
        assert db.get(Plan, old).status == "superseded"

    assert client.post("/api/alerts/99999/generate", json={}).status_code == 404
    assert client.post("/api/alerts/6/reject", json={"reason_code": "unfair"}).status_code == 200
    assert client.post("/api/alerts/6/generate", json={}).status_code == 409  # rejected alerts cannot be planned


def test_approve_over_http_then_conflicts_and_idempotency(api, adb):
    client, set_llm, factory = api
    generate(client, set_llm, adb, sizes=(2,))
    plan_id = client.get(f"/api/alerts/{ALERT_ID}").json()["presented"][0]["id"]
    resp = client.post(f"/api/plans/{plan_id}/approve", json={"actor_trainer_id": 1})
    assert resp.status_code == 200 and resp.json()["status"] == "approved" and len(resp.json()["outbox"]) == 2
    again = client.post(f"/api/plans/{plan_id}/approve", json={"actor_trainer_id": 1})
    assert again.status_code == 200 and again.json()["already_approved"] is True

    detail = client.get(f"/api/alerts/{ALERT_ID}").json()
    assert detail["alert"]["status"] == "approved" and detail["decisions"][0]["decision"] == "approve"
    assert detail["decisions"][0]["actor_name"]
    assert client.post("/api/plans/99999/approve", json={}).status_code == 404


def test_stale_approve_returns_409_with_violations(api, adb):
    client, set_llm, factory = api
    from app.models import TrainingSession
    generate(client, set_llm, adb, sizes=(1,))
    detail = client.get(f"/api/alerts/{ALERT_ID}").json()
    session_id = detail["presented"][0]["changes"][0]["session_id"]
    with factory() as db:
        db.get(TrainingSession, session_id).capacity = 0
        db.commit()
    resp = client.post(f"/api/plans/{detail['presented'][0]['id']}/approve", json={"actor_trainer_id": 1})
    body = resp.json()
    assert resp.status_code == 409 and body["reason"] == "stale" and body["violations"][0]["code"]
    assert client.get(f"/api/alerts/{ALERT_ID}").json()["alert"]["status"] == "plans_ready"


def test_reject_endpoints_validate_input(api, adb):
    client, set_llm, _ = api
    generate(client, set_llm, adb, sizes=(1,))
    plan_id = client.get(f"/api/alerts/{ALERT_ID}").json()["presented"][0]["id"]
    assert client.post(f"/api/plans/{plan_id}/reject", json={"actor_trainer_id": 1}).status_code == 422
    assert client.post(f"/api/plans/{plan_id}/reject", json={"reason_code": "other"}).status_code == 422
    ok = client.post(f"/api/plans/{plan_id}/reject", json={"actor_trainer_id": 1, "reason_code": "unfair"})
    assert ok.status_code == 200 and ok.json()["alert_status"] == "rejected"  # the only recommended plan is gone
    assert client.post("/api/alerts/6/dismiss", json={"actor_trainer_id": 1}).json()["alert_status"] == "dismissed"


def test_weekly_sync_keeps_one_active_alert_and_rejected_stays_closed(adb):
    alert = adb.get(Alert, ALERT_ID)
    alert.status = "plans_ready"
    adb.commit()
    forecasts = compute_forecasts(load_facts(adb))
    changes = sync_alerts(adb, forecasts, datetime(2026, 5, 22))
    assert ALERT_ID not in changes.opened and not [a for a in changes.opened if adb.get(Alert, a).course_id == alert.course_id]

    alert.status = "rejected"
    adb.commit()
    changes = sync_alerts(adb, forecasts, datetime(2026, 5, 22))
    assert not [a for a in changes.opened if adb.get(Alert, a).course_id == alert.course_id]  # same risk level: not reopened
    assert adb.scalars(select(Alert).where(Alert.course_id == alert.course_id, Alert.status == "open")).first() is None
