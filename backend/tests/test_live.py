"""Phase 7: the live event bus, the SSE stream and simulation auto-play. All offline."""
import asyncio
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.live import autoplay as autoplay_mod
from app.live.autoplay import Autoplay, advance_once
from app.live.bus import BUFFER_SIZE, QUEUE_SIZE, bus, publish_after_commit
from app.llm.fake import FakeLLMClient
from app.llm.planner import run_planner
from app.models import PlannerRun
from app.services import decisions
from app.simulator import engine as sim_engine
from tests.test_decisions import TRAINER, make_run
from tests.test_planner import ALERT_ID, good_plan, submit


@pytest.fixture(autouse=True)
def fresh_bus():
    bus.reset()
    yield
    bus.reset()


def types() -> list[str]:
    return [e.type for e in bus._buffer]


def drain(sub) -> list:
    out = []
    while not sub.queue.empty():
        out.append(sub.queue.get_nowait())
    return out


# --- Bus -------------------------------------------------------------------------------------------------
def test_publish_from_async_code_and_from_a_thread_reaches_subscribers():
    async def scenario():
        bus.attach_loop(asyncio.get_running_loop())
        sub = bus.subscribe()
        bus.publish("sim.advanced", {"n": 1})
        threading.Thread(target=bus.publish, args=("forecast.updated", {"course_ids": [1]})).start()
        first, second = await asyncio.wait_for(sub.queue.get(), 2), await asyncio.wait_for(sub.queue.get(), 2)
        return [first, second]

    events = asyncio.run(scenario())
    assert {e.type for e in events} == {"sim.advanced", "forecast.updated"} and events[0].id < events[1].id


def test_events_get_increasing_ids():
    ids = [bus.publish("heartbeat").id for _ in range(3)]
    assert ids == [1, 2, 3]


def test_nothing_is_published_when_the_transaction_rolls_back(db):
    db.execute(select(1))  # a real service has touched the database (so a transaction is open) before it queues events
    publish_after_commit(db, "sessions.changed", {"session_ids": [1]})
    db.rollback()
    db.commit()
    assert types() == []
    publish_after_commit(db, "sessions.changed", {"session_ids": [1]})
    assert types() == []  # not before the commit
    db.commit()
    assert types() == ["sessions.changed"]


def test_events_of_one_transaction_are_merged(db):
    publish_after_commit(db, "sessions.changed", {"session_ids": [1, 2]})
    publish_after_commit(db, "sessions.changed", {"session_ids": [2, 3]})
    db.commit()
    assert [e.data for e in bus._buffer] == [{"session_ids": [1, 2, 3]}]


def test_last_event_id_replays_what_was_missed():
    async def scenario():
        bus.attach_loop(asyncio.get_running_loop())
        for i in range(5):
            bus.publish("sim.advanced", {"i": i})
        sub = bus.subscribe(last_event_id=3)
        return drain(sub)

    assert [e.id for e in asyncio.run(scenario())] == [4, 5]


def test_an_id_older_than_the_buffer_gets_resync():
    async def scenario():
        for _ in range(BUFFER_SIZE + 50):
            bus.publish("heartbeat")
        return drain(bus.subscribe(last_event_id=3))

    events = asyncio.run(scenario())
    assert [e.type for e in events] == ["resync"] and events[0].data["reason"] == "stale_last_event_id"


def test_an_id_from_before_a_server_restart_gets_resync():
    async def scenario():
        bus.publish("heartbeat")
        return drain(bus.subscribe(last_event_id=500))

    assert [e.type for e in asyncio.run(scenario())] == ["resync"]


def test_a_full_queue_gives_that_subscriber_a_resync_and_nobody_else():
    async def scenario():
        bus.attach_loop(asyncio.get_running_loop())
        slow, fast = bus.subscribe(), bus.subscribe()
        for i in range(QUEUE_SIZE + 5):
            bus.publish("sim.advanced", {"i": i})
            await asyncio.sleep(0)
            if i < QUEUE_SIZE - 1:
                drain(fast)  # the fast client keeps up
        await asyncio.sleep(0.05)
        return drain(slow), drain(fast)

    slow, fast = asyncio.run(scenario())
    assert slow[0].type == "resync" and slow[0].data["reason"] == "overflow"
    assert all(e.type == "sim.advanced" for e in fast) and fast


# --- SSE endpoint over a real socket (an in-process ASGI client would buffer the endless response) ----------
@pytest.fixture()
def live_server(monkeypatch):
    from app.main import app

    monkeypatch.setattr("app.config.settings.live_heartbeat_seconds", 0.2)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, lifespan="off", log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


def read_stream(url: str, wanted: set[str], publish=None, headers=None, timeout=5.0) -> tuple[list[str], httpx.Response]:
    seen: list[str] = []
    deadline = time.time() + timeout
    with httpx.Client(timeout=timeout) as client:
        with client.stream("GET", url + "/api/live/stream", headers=headers or {}) as response:
            if publish:
                threading.Timer(0.3, publish).start()
            for line in response.iter_lines():
                if line.startswith("event:"):
                    seen.append(line.split(":", 1)[1].strip())
                if wanted <= set(seen) or time.time() > deadline:
                    break
            return seen, response


def test_stream_sends_heartbeats_and_typed_events(live_server):
    seen, response = read_stream(live_server, {"heartbeat", "sim.advanced"}, publish=lambda: bus.publish("sim.advanced", {"x": 1}))
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache" and response.headers["x-accel-buffering"] == "no"
    assert {"heartbeat", "sim.advanced"} <= set(seen)


def test_stream_replays_after_reconnect_with_last_event_id(live_server):
    first = bus.publish("sim.advanced", {"n": 1})
    bus.publish("alert.created", {"alert_id": 1})
    seen, _ = read_stream(live_server, {"alert.created"}, headers={"Last-Event-ID": str(first.id)})
    assert "alert.created" in seen and "sim.advanced" not in seen
    seen, _ = read_stream(live_server, {"resync"}, headers={"Last-Event-ID": "9999"})
    assert "resync" in seen


def test_the_subscriber_is_removed_when_the_client_disconnects(live_server):
    read_stream(live_server, {"heartbeat"})
    for _ in range(50):
        if bus.subscriber_count == 0:
            break
        time.sleep(0.1)
    assert bus.subscriber_count == 0


# --- Who publishes what ----------------------------------------------------------------------------------
def test_simulator_advance_publishes(adb):
    sim_engine.advance(adb, 3)
    assert {"sim.advanced", "sessions.changed"} <= set(types())


def test_advance_over_a_week_publishes_forecast_and_attendance(adb):
    sim_engine.advance(adb, 7)
    assert {"sim.advanced", "attendance.recorded", "forecast.updated"} <= set(types())


def test_approve_and_reject_publish(adb):
    _, ranked = make_run(adb, sizes=(2, 3))
    bus.reset()
    decisions.approve_plan(adb, ranked[0].id, TRAINER)
    assert {"plan.decided", "sessions.changed", "alert.updated"} <= set(types())
    assert next(e for e in bus._buffer if e.type == "plan.decided").data["decision"] == "approve"


def test_reject_publishes_plan_decided(adb):
    _, ranked = make_run(adb, sizes=(2, 3))
    bus.reset()
    decisions.reject_plan(adb, ranked[0].id, TRAINER, "other", "no")
    assert "plan.decided" in types()


def test_a_failed_approve_publishes_nothing(adb):
    _, ranked = make_run(adb)
    decisions.approve_plan(adb, ranked[0].id, TRAINER)
    bus.reset()
    with pytest.raises(Exception):
        decisions.reject_plan(adb, ranked[0].id, TRAINER, "other", "too late")
    assert "plan.decided" not in types()


def test_planner_publishes_progress_and_completion(adb):
    plans = [good_plan(adb, how_many=2), good_plan(adb, how_many=3)]
    run_planner(adb, ALERT_ID, 2, FakeLLMClient([submit(p) for p in plans]))
    progress = [e.data["samples_done"] for e in bus._buffer if e.type == "planner_run.progress"]
    assert progress == [0, 1, 2] and types().count("planner_run.completed") == 1
    assert "alert.updated" in types()


def test_crud_publishes(client):
    assert client.post("/api/drivers", json={"name": "Test Driver", "nationality": "UAE", "shift": "day", "depot": "Dubai", "hire_date": "2024-01-01"}).status_code in (200, 201)
    assert any(e.type == "master_data.changed" and e.data["entity"] == "drivers" for e in bus._buffer)


# --- Auto-play -------------------------------------------------------------------------------------------
def test_autoplay_advances_on_the_interval():
    calls = []
    ap = Autoplay(step_fn=lambda days: calls.append(days))

    async def scenario():
        await ap.start("week", 0.05)
        await asyncio.sleep(0.4)
        await ap.stop()

    asyncio.run(scenario())
    assert len(calls) >= 3 and set(calls) == {7} and ap.steps_done == len(calls) and not ap.enabled


def test_autoplay_pauses_with_a_reason():
    ap = Autoplay(step_fn=lambda days: "planner_running")

    async def scenario():
        await ap.start("day", 0.05)
        await asyncio.sleep(0.3)

    asyncio.run(scenario())
    assert not ap.enabled and ap.paused_reason == "planner_running"
    assert [e.data["reason"] for e in bus._buffer if e.type == "sim.autoplay_paused"] == ["planner_running"]


def test_autoplay_pauses_when_the_advance_fails():
    def boom(days):
        raise RuntimeError("db is down")

    ap = Autoplay(step_fn=boom)

    async def scenario():
        await ap.start("day", 0.05)
        await asyncio.sleep(0.3)

    asyncio.run(scenario())
    assert ap.paused_reason.startswith("error: RuntimeError")


def test_advance_once_pauses_while_a_planner_run_is_active(aengine):
    with Session(aengine) as db:
        db.add(PlannerRun(alert_id=1, course_id=1, status="running", n_requested=1))
        db.commit()
    assert advance_once(lambda: Session(aengine), 1) == "planner_running"


def test_advance_once_pauses_at_the_end_of_the_year(aengine, monkeypatch):
    from app.services.sim_clock import set_sim_now
    from app.simulator.rules import SIM_END

    with Session(aengine) as db:
        set_sim_now(db, SIM_END)
        db.commit()
    assert advance_once(lambda: Session(aengine), 1) == "year_end"


def test_advance_once_moves_the_clock(aengine):
    from app.services.sim_clock import get_sim_now

    with Session(aengine) as db:
        before = get_sim_now(db)
    assert advance_once(lambda: Session(aengine), 1) is None
    with Session(aengine) as db:
        assert (get_sim_now(db) - before).days == 1


def test_autoplay_endpoints(client):
    assert client.get("/api/sim/autoplay").json()["enabled"] is False
    assert client.post("/api/sim/autoplay", json={"enabled": True, "interval_seconds": 1}).status_code == 422  # below the 3 s floor
    assert client.post("/api/sim/autoplay", json={"enabled": False}).json()["enabled"] is False
