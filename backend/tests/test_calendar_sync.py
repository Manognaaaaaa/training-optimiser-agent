"""Phase 7: the outbox worker, drift handling, reconcile and reset. Everything runs against FakeCalendarClient."""
import logging
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.calendar import ops, sync
from app.calendar.base import NotFound, PermanentError, RateLimited, TransientError
from app.calendar.factory import CalendarBackend, build_backend
from app.calendar.fake import FakeCalendarClient
from app.calendar.mapping import event_id_for
from app.config import Settings
from app.models import AuditLog, CalendarLink, CalendarOutbox, TrainingSession
from app.services import decisions
from tests.test_decisions import TRAINER, make_run

CAL = "sandbox"


@pytest.fixture()
def backend():
    return CalendarBackend(FakeCalendarClient(), "fake", CAL)


@pytest.fixture()
def sessions(adb):
    return list(adb.scalars(select(TrainingSession).where(TrainingSession.status == "scheduled").order_by(TrainingSession.id).limit(4)))


def queue(db, session, op="upsert", source="manual"):
    row = sync.enqueue(db, session.id, source=source, op=op)
    db.commit()
    return row


def change(db, session):
    """Make the session's event body different (a bigger room)."""
    session.capacity += 1
    db.commit()


def audit(db, action):
    return list(db.scalars(select(AuditLog).where(AuditLog.action == action)))


def test_approve_to_google_end_to_end(adb, backend):
    plans, ranked = make_run(adb)
    decisions.approve_plan(adb, ranked[0].id, TRAINER)
    rows = list(adb.scalars(select(CalendarOutbox).where(CalendarOutbox.plan_id == ranked[0].id)))
    assert rows and all(r.session_id and r.alert_id and r.source == "plan_approval" for r in rows)

    sync.process_due(adb, backend)

    session_ids = {r.session_id for r in rows}
    assert len(backend.client.events) == len(session_ids)
    for sid in session_ids:
        event = backend.client.events[(CAL, event_id_for(sid))]
        session = adb.get(TrainingSession, sid)
        assert event["start"]["dateTime"] == session.start_time.isoformat(timespec="seconds")
        assert event["extendedProperties"]["private"]["plan_id"] == str(ranked[0].id)
        link = adb.scalar(select(CalendarLink).where(CalendarLink.session_id == sid))
        assert link.sync_status == "synced" and link.html_link and link.etag and link.synced_hash
    assert {r.status for r in adb.scalars(select(CalendarOutbox))} <= {"done", "skipped"}
    assert audit(adb, "calendar.sync_ok")


def test_second_run_makes_no_api_calls(adb, backend, sessions):
    queue(adb, sessions[0])
    sync.process_due(adb, backend)
    calls = len(backend.client.calls)
    queue(adb, sessions[0])
    sync.process_due(adb, backend)
    assert len(backend.client.calls) == calls  # the hash matched
    assert adb.scalars(select(CalendarOutbox).order_by(CalendarOutbox.id.desc())).first().status == "done"


def test_pending_rows_for_one_session_are_coalesced(adb, backend, sessions):
    rows = [queue(adb, sessions[0]) for _ in range(3)]
    sync.process_due(adb, backend)
    assert len(backend.client.calls_to("insert")) == 1 and len(backend.client.calls) == 1
    assert [adb.get(CalendarOutbox, r.id).status for r in rows] == ["skipped", "skipped", "done"]


def test_already_exists_recovers_with_get_and_update(adb, backend, sessions):
    s = sessions[0]
    backend.client.put_raw(CAL, event_id_for(s.id), {"summary": "left over from a crash"})
    queue(adb, s)
    sync.process_due(adb, backend)
    assert [c[0] for c in backend.client.calls] == ["insert", "get", "update"]
    assert backend.client.events[(CAL, event_id_for(s.id))]["summary"] != "left over from a crash"
    assert adb.scalar(select(CalendarLink).where(CalendarLink.session_id == s.id)).sync_status == "synced"


def test_precondition_failed_records_drift_then_overwrites(adb, backend, sessions):
    s = sessions[0]
    queue(adb, s)
    sync.process_due(adb, backend)
    backend.client.external_edit(CAL, event_id_for(s.id), summary="edited by a human")
    change(adb, s)
    queue(adb, s)
    sync.process_due(adb, backend)
    assert len(audit(adb, "calendar.drift_detected")) == 1
    assert backend.client.events[(CAL, event_id_for(s.id))]["summary"] != "edited by a human"
    assert adb.scalar(select(CalendarLink).where(CalendarLink.session_id == s.id)).sync_status == "synced"


def test_event_deleted_in_google_is_reinserted(adb, backend, sessions):
    s = sessions[0]
    queue(adb, s)
    sync.process_due(adb, backend)
    backend.client.forget(CAL, event_id_for(s.id))  # NotFound on the next update
    change(adb, s)
    queue(adb, s)
    sync.process_due(adb, backend)
    assert (CAL, event_id_for(s.id)) in backend.client.events and len(backend.client.calls_to("insert")) == 2


def test_event_deleted_through_the_api_is_restored(adb, backend, sessions):
    s = sessions[0]
    queue(adb, s)
    sync.process_due(adb, backend)
    backend.client.external_delete(CAL, event_id_for(s.id))  # Gone; a plain insert would now give AlreadyExists
    change(adb, s)
    queue(adb, s)
    sync.process_due(adb, backend)
    assert (CAL, event_id_for(s.id)) in backend.client.events


def test_rate_limited_twice_then_success_backs_off(adb, backend, sessions):
    s = sessions[0]
    backend.client.fail_next(2, RateLimited, "insert")
    row = queue(adb, s)

    sync.process_due(adb, backend)
    row = adb.get(CalendarOutbox, row.id)
    assert (row.status, row.attempts) == ("pending", 1) and "RateLimited" in row.last_error
    wait = (row.next_attempt_at - sync.utcnow()).total_seconds()
    assert 1.2 < wait <= 2.5  # 2 s with jitter

    sync.process_due(adb, backend)  # not due yet: nothing happens
    assert adb.get(CalendarOutbox, row.id).attempts == 1

    row.next_attempt_at = None
    adb.commit()
    sync.process_due(adb, backend)
    row = adb.get(CalendarOutbox, row.id)
    assert row.attempts == 2 and 2.5 < (row.next_attempt_at - sync.utcnow()).total_seconds() <= 5  # 4 s with jitter

    row.next_attempt_at = None
    adb.commit()
    sync.process_due(adb, backend)
    row = adb.get(CalendarOutbox, row.id)
    assert (row.status, row.attempts) == ("done", 3) and row.last_error is None


def test_permanent_error_fails_at_once(adb, backend, sessions):
    s = sessions[0]
    backend.client.fail_next(1, PermanentError, "insert")
    row = queue(adb, s)
    sync.process_due(adb, backend)
    row = adb.get(CalendarOutbox, row.id)
    assert (row.status, row.attempts) == ("failed", 1)
    assert adb.scalar(select(CalendarLink).where(CalendarLink.session_id == s.id)).sync_status == "failed"
    assert audit(adb, "calendar.sync_failed")


def test_max_attempts_ends_in_failed(adb, backend, sessions, monkeypatch):
    monkeypatch.setattr("app.config.settings.calendar_max_attempts", 2)
    backend.client.fail_next(10, TransientError, "insert")
    row = queue(adb, sessions[0])
    sync.process_due(adb, backend)
    adb.get(CalendarOutbox, row.id).next_attempt_at = None
    adb.commit()
    sync.process_due(adb, backend)
    row = adb.get(CalendarOutbox, row.id)
    assert (row.status, row.attempts) == ("failed", 2)


def test_cancel_of_a_missing_event_counts_as_success(adb, backend, sessions):
    s = sessions[0]
    row = queue(adb, s, op="cancel")
    sync.process_due(adb, backend)
    assert adb.get(CalendarOutbox, row.id).status == "done"


def test_cancelled_session_removes_the_event(adb, backend, sessions):
    s = sessions[0]
    queue(adb, s)
    sync.process_due(adb, backend)
    s.status = "cancelled"
    queue(adb, s)
    sync.process_due(adb, backend)
    assert (CAL, event_id_for(s.id)) not in backend.client.events
    assert adb.scalar(select(CalendarLink).where(CalendarLink.session_id == s.id)).sync_status == "cancelled"


def test_stuck_in_progress_rows_are_recovered(adb, sessions):
    row = queue(adb, sessions[0])
    row.status, row.claimed_at = "in_progress", sync.utcnow() - timedelta(minutes=5)
    fresh = queue(adb, sessions[1])
    fresh.status, fresh.claimed_at = "in_progress", sync.utcnow()
    adb.commit()
    assert sync.recover_stuck(adb) == 1
    assert adb.get(CalendarOutbox, row.id).status == "pending" and adb.get(CalendarOutbox, fresh.id).status == "in_progress"


def test_reset_only_deletes_events_with_our_tags(adb, backend, sessions, monkeypatch):
    queue(adb, sessions[0])
    sync.process_due(adb, backend)
    backend.client.put_raw(CAL, "someoneelses", {"summary": "lunch"})
    backend.client.put_raw(CAL, "otherenvtest", {"summary": "x", "extendedProperties": {"private": {"app": "training-optimiser", "env": "alice"}}})
    with pytest.raises(Exception):
        ops.reset_calendar(adb, backend, "yes")
    result = ops.reset_calendar(adb, backend, "DELETE")
    assert result["events_deleted"] == 1 and result["links_cleared"] == 1
    assert set(k[1] for k in backend.client.events) == {"someoneelses", "otherenvtest"}
    assert not list(adb.scalars(select(CalendarLink))) and audit(adb, "calendar.reset")


def test_reconcile_reports_missing_orphaned_and_mismatched(adb, backend, sessions):
    for s in sessions[:3]:
        queue(adb, s)
    sync.process_due(adb, backend)
    backend.client.forget(CAL, event_id_for(sessions[0].id))                              # missing in Google
    backend.client.external_edit(CAL, event_id_for(sessions[1].id), summary="changed")    # content mismatch
    backend.client.put_raw(CAL, "orphan1", {                                              # orphaned
        "summary": "x", "start": {"dateTime": sessions[2].start_time.isoformat()},
        "extendedProperties": {"private": {"app": "training-optimiser", "env": "dev", "session_id": "999999"}}})
    start = min(s.start_time for s in sessions[:3]) - timedelta(days=1)
    end = max(s.start_time for s in sessions[:3]) + timedelta(days=1)

    report = ops.reconcile(adb, backend, start, end)

    assert [m["session_id"] for m in report["missing_in_google"]] == [sessions[0].id]
    assert [m["session_id"] for m in report["content_mismatch"]] == [sessions[1].id]
    assert [o["event_id"] for o in report["orphaned_in_google"]] == ["orphan1"]
    assert report["in_sync"] == 1
    assert adb.scalar(select(CalendarLink).where(CalendarLink.session_id == sessions[1].id)).sync_status == "drift"


def test_backfill_is_limited_to_eight_weeks(adb, sessions):
    start = sessions[0].start_time.replace(hour=0)
    with pytest.raises(Exception):
        ops.backfill(adb, start, start + timedelta(days=60))
    result = ops.backfill(adb, start, start + timedelta(days=7))
    assert result["enqueued"] >= 1


def test_google_provider_without_config_falls_back_to_fake(caplog):
    cfg = Settings(calendar_provider="google", google_service_account_file="", google_calendar_id="")
    with caplog.at_level(logging.WARNING):
        backend = build_backend(cfg)
    assert backend.provider == "fake" and isinstance(backend.client, FakeCalendarClient)
    assert "falling back" in caplog.text


def test_session_payloads_carry_the_calendar_state(client, engine):
    from sqlalchemy.orm import Session

    with Session(engine) as db:
        s = db.scalars(select(TrainingSession).where(TrainingSession.status == "scheduled").limit(1)).one()
        sid = s.id
        sync.enqueue(db, sid, source="manual")
        db.commit()
    assert client.get(f"/api/sessions/{sid}").json()["calendar"]["status"] == "pending"
    assert client.get(f"/api/sessions?page_size=2").json()["items"][0]["calendar"]["status"] in {"none", "pending"}


def test_sync_status_endpoint(client):
    body = client.get("/api/calendar/sync/status").json()
    assert body["provider"] in {"fake", "google"} and set(body["counts"]) >= {"pending", "done", "failed", "skipped"}
    assert client.post("/api/calendar/reset", json={"confirm": "no"}).status_code == 422
    assert client.get("/api/calendar/sync/failed").json() == []
