"""Phase 7 live smoke test: a real Google sandbox calendar plus the live event stream. Writes docs/calendar_sync_report.md.

Before running: set up Google (docs/google_calendar_setup.md), put CALENDAR_PROVIDER=google and the key/calendar id in
backend/.env, start the backend (ONE worker), then:

    cd backend
    python -m scripts.smoke_phase7                       # API on http://localhost:8000/api
    python -m scripts.smoke_phase7 --allow-fake          # rehearsal with the fake calendar (skips the Google read-back)

What it does:
  1. queue a 1-week backfill and wait for the outbox to drain
  2. approve the top plan of one alert that has plans waiting (skipped when there is none)
  3. read every synced event back from Google and compare it with the database
  4. reconcile (expect no drift), edit one event directly in Google, reconcile (expect 1), sync again (expect fixed)
  5. open the live stream, advance the simulation and check that sim.advanced and attendance.recorded arrive
  6. write the report, then ASK before resetting the sandbox calendar
"""
import argparse
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from app.calendar.factory import build_backend
from app.calendar.mapping import event_id_for
from app.config import settings

REPORT = Path(__file__).resolve().parents[2] / "docs" / "calendar_sync_report.md"
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")
    return ok


def wait_drained(api: httpx.Client, timeout: float = 120) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = api.get("/calendar/sync/status").json()
        if status["counts"]["pending"] + status["counts"]["in_progress"] == 0:
            return status
        time.sleep(1)
    raise TimeoutError("the outbox did not drain in time")


def listen(base: str, seen: list[tuple[str, float, float | None]], stop: threading.Event, ready: threading.Event) -> None:
    """Collect (event type, time received, server timestamp) from the SSE stream."""
    import json

    try:
        with httpx.Client(timeout=None) as client:
            with client.stream("GET", f"{base}/live/stream") as response:
                ready.set()
                kind = None
                for line in response.iter_lines():
                    if stop.is_set():
                        return
                    if line.startswith("event:"):
                        kind = line.split(":", 1)[1].strip()
                    elif line.startswith("data:") and kind:
                        try:
                            ts = json.loads(line.split(":", 1)[1]).get("_ts")
                        except ValueError:
                            ts = None
                        seen.append((kind, time.time(), ts))
    except httpx.HTTPError:
        ready.set()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api", default="http://localhost:8000/api")
    parser.add_argument("--allow-fake", action="store_true", help="run against the fake calendar (no Google read-back)")
    args = parser.parse_args()
    api = httpx.Client(base_url=args.api, timeout=60)

    status = api.get("/calendar/sync/status").json()
    provider = status["provider"]
    print(f"Server provider: {provider}   calendar: {status['calendar_id']}")
    if provider != "google" and not args.allow_fake:
        print("The server is not using Google. Set CALENDAR_PROVIDER=google in backend/.env and restart, or pass --allow-fake.")
        return 2
    google = build_backend(settings) if provider == "google" else None
    if google is not None and google.provider != "google":
        print("This script could not start the Google client from backend/.env.")
        return 2
    start_stats = status["stats"]

    # --- 1. backfill one week --------------------------------------------------------------------------------------
    sim_now = datetime.fromisoformat(api.get("/sim/state").json()["current_time"])
    week_from, week_to = sim_now + timedelta(days=1), sim_now + timedelta(days=8)
    fill = api.post("/calendar/backfill", json={"from_date": week_from.isoformat(), "to_date": week_to.isoformat()}).json()
    session_ids = list(fill["session_ids"])
    check("backfill queued sessions", fill["enqueued"] > 0, f"({fill['enqueued']} sessions)")
    wait_drained(api)

    # --- 2. approve the top plan of one alert -----------------------------------------------------------------------
    approve_seconds = None
    ready = api.get("/alerts", params={"status": "plans_ready"}).json()
    if ready:
        detail = api.get(f"/alerts/{ready[0]['id']}").json()
        plan = detail["presented"][0]
        t0 = time.time()
        approved = api.post(f"/plans/{plan['id']}/approve", json={"actor_trainer_id": 1})
        if approved.status_code == 200:
            wait_drained(api)
            approve_seconds = time.time() - t0
            touched = api.get(f"/alerts/{ready[0]['id']}").json()["calendar"]["sessions"]
            session_ids += [s["session_id"] for s in touched]
            check("approved plan reached the calendar", all(s["calendar"]["status"] == "synced" for s in touched),
                  f"({len(touched)} sessions in {approve_seconds:.1f}s)")
        else:
            check("approve the top plan", False, approved.text)
    else:
        print("No alert has plans waiting, so the approve step is skipped (generate plans on an alert first to cover it).")
    session_ids = sorted(set(session_ids))

    # --- 3. read back from Google ----------------------------------------------------------------------------------
    mismatches = 0
    if google is not None:
        for sid in session_ids:
            row = api.get(f"/sessions/{sid}").json()
            event = google.client.get_event(google.calendar_id, event_id_for(sid))
            bad = []
            if event["start"]["dateTime"][:19] != row["start_time"][:19]:
                bad.append("start")
            if event["end"]["dateTime"][:19] != row["end_time"][:19]:
                bad.append("end")
            if event.get("location") != row["location"]:
                bad.append("location")
            if not event.get("summary", "").startswith(row["course_code"]):
                bad.append("summary")
            if event.get("extendedProperties", {}).get("private", {}).get("session_id") != str(sid):
                bad.append("session_id tag")
            mismatches += bool(bad)
            if bad:
                print(f"  session {sid} differs in: {', '.join(bad)}")
        check("Google events match the database", mismatches == 0, f"({len(session_ids)} events checked)")

    # --- 4. reconcile, drift, fix ---------------------------------------------------------------------------------
    window = {"from": (week_from - timedelta(days=1)).isoformat(), "to": (week_to + timedelta(days=1)).isoformat()}
    first = api.get("/calendar/reconcile", params=window).json()
    check("reconcile finds no drift", not first["content_mismatch"] and not first["missing_in_google"],
          f"(in sync {first['in_sync']}, missing {len(first['missing_in_google'])}, edited {len(first['content_mismatch'])}, "
          f"orphaned {len(first['orphaned_in_google'])})")
    second_report = first
    if google is not None and session_ids:
        target = session_ids[0]
        eid = event_id_for(target)
        event = google.client.get_event(google.calendar_id, eid)
        event["summary"] = event["summary"] + " (edited by smoke test)"
        google.client.update_event(google.calendar_id, eid, event)
        second_report = api.get("/calendar/reconcile", params=window).json()
        check("reconcile sees the edit", [m["session_id"] for m in second_report["content_mismatch"]] == [target])
        api.post(f"/calendar/sync/session/{target}")
        wait_drained(api)
        third = api.get("/calendar/reconcile", params=window).json()
        check("sync fixed the edit", not third["content_mismatch"])

    # --- 5. live stream -------------------------------------------------------------------------------------------
    seen: list[tuple[str, float, float | None]] = []
    stop, ready_evt = threading.Event(), threading.Event()
    threading.Thread(target=listen, args=(args.api, seen, stop, ready_evt), daemon=True).start()
    ready_evt.wait(5)
    time.sleep(0.5)
    for _ in range(7):  # a day with no finished sessions has no attendance: keep going up to a week
        api.post("/sim/advance", json={"days": 1})
        time.sleep(1)
        if any(k == "attendance.recorded" for k, _, _ in seen):
            break
    stop.set()
    kinds = {k for k, _, _ in seen}
    check("sim.advanced arrived over the stream", "sim.advanced" in kinds)
    check("attendance.recorded arrived over the stream", "attendance.recorded" in kinds)
    latencies = [(received - ts) * 1000 for _, received, ts in seen if ts is not None and received >= ts]

    # --- 6. report ------------------------------------------------------------------------------------------------
    end = api.get("/calendar/sync/status").json()["stats"]

    def delta(key: str) -> int:
        return end[key] - start_stats[key]

    times = end["approve_to_synced_seconds"]
    lines = [
        "# Calendar sync report", "",
        f"Generated {datetime.now():%Y-%m-%d %H:%M} by `python -m scripts.smoke_phase7` · provider `{provider}` · environment tag `{settings.app_env}`", "",
        "## Sync", "",
        f"- Events synced: {delta('synced')}", f"- API calls made: {delta('api_calls')}",
        f"- Calls skipped because nothing changed (hash match): {delta('skipped_by_hash')}",
        f"- Retries: {delta('retries')}", f"- Failures: {delta('failures')}", f"- Edits in Google detected (drift): {delta('drift')}",
        f"- Average time from queue to synced: {sum(times) / len(times):.1f} s" if times else "- Average time from queue to synced: no data",
        f"- Approve to synced (this run): {approve_seconds:.1f} s" if approve_seconds is not None else "- Approve to synced: skipped (no plan was waiting)",
        "", "## Reconcile", "",
        f"- Before the edit: in sync {first['in_sync']}, missing {len(first['missing_in_google'])}, edited {len(first['content_mismatch'])}, orphaned {len(first['orphaned_in_google'])}",
        f"- After editing one event in Google: edited {len(second_report['content_mismatch'])}",
        "", "## Live stream", "",
        f"- Events received: {len(seen)} ({', '.join(sorted(kinds))})",
        (f"- Publish to receive latency: median {sorted(latencies)[len(latencies) // 2]:.0f} ms, max {max(latencies):.0f} ms over {len(latencies)} events"
         if latencies else "- Publish to receive latency: no data"),
        "", "## Checks", "", *[f"- {'PASS' if ok else 'FAIL'}: {name} {detail}" for name, ok, detail in results], "",
    ]
    REPORT.parent.mkdir(exist_ok=True)
    REPORT.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nReport written to {REPORT}")

    if google is not None and input("\nReset the sandbox calendar now (delete every event this app created)? Type DELETE to do it: ").strip() == "DELETE":
        print(api.post("/calendar/reset", json={"confirm": "DELETE"}).json())
    return 0 if all(ok for _, ok, _ in results) else 1


if __name__ == "__main__":
    sys.exit(main())
