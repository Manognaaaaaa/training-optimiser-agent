"""Validation report for the synthetic data generator.

Run from backend/:  python -m app.simulator.report [--seed 42]
Writes docs/synthetic_data_report.md. The full-year dry run happens in a throw-away in-memory
database built from the same seed, so training.db is never touched.
"""
import argparse
import hashlib
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.models import (
    AuditLog, Course, Driver, DriverUnavailability, Enrollment, Trainer, TrainingSession, TrainingTarget,
)
from app.simulator import engine as sim_engine
from app.simulator import scenarios
from app.simulator.rules import (
    BOOKING_WINDOW_DAYS, RAMADAN_END, RAMADAN_START, REST_HOURS, SIM_START, SUMMER_PEAK_END, SUMMER_PEAK_START,
    is_ramadan, is_summer_peak, week_start,
)
from app.simulator.seed import reseed, table_counts

POOLED_EXTRA_SEEDS = 4  # extra seeds used to pool the seasonal check
REPORT_PATH = Path(__file__).resolve().parents[3] / "docs" / "synthetic_data_report.md"


def _fresh_engine():
    return create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})


def fingerprint(db: Session) -> str:
    """Hash of all generated rows, used to prove the same seed gives identical data."""
    h = hashlib.sha256()
    for model, cols in (
        (Driver, ("id", "employee_code", "name", "nationality", "shift", "depot", "hire_date", "is_active")),
        (Trainer, ("id", "name", "max_sessions_per_week")),
        (TrainingSession, ("id", "course_id", "trainer_id", "start_time", "end_time", "capacity", "location")),
        (Enrollment, ("id", "session_id", "driver_id", "status")),
        (DriverUnavailability, ("id", "driver_id", "start_time", "end_time", "reason")),
    ):
        for row in db.scalars(select(model).order_by(model.id)):
            h.update(repr([getattr(row, c) for c in cols]).encode())
    return h.hexdigest()[:16]


def _md_table(headers: list[str], rows: list[list]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return "\n".join(lines)


def _pct(part: float, whole: float) -> str:
    return f"{100 * part / whole:.1f}%" if whole else "n/a"


def _distribution(counter: Counter) -> str:
    total = sum(counter.values())
    rows = [[k, v, _pct(v, total)] for k, v in counter.most_common()]
    return _md_table(["Value", "Count", "Share"], rows)


# --- Section 1: what the seed produced ----------------------------------------------------
def seed_section(db: Session) -> str:
    drivers = list(db.scalars(select(Driver)))
    courses = list(db.scalars(select(Course).order_by(Course.id)))
    trainers = list(db.scalars(select(Trainer).order_by(Trainer.id)))
    targets = {t.course_id: t.target_completions for t in db.scalars(select(TrainingTarget))}
    sessions = list(db.scalars(select(TrainingSession)))
    booked = Counter(e.session_id for e in db.scalars(select(Enrollment)))

    out = ["## 1. What the seed produced", "", "### Row counts", ""]
    out.append(_md_table(["Table", "Rows"], [[t, n] for t, n in table_counts(db).items()]))
    out += ["", f"Drivers hired during 2026 (inactive at seed): {sum(1 for d in drivers if not d.is_active)}", ""]
    out += ["### Nationality", "", _distribution(Counter(d.nationality for d in drivers)), ""]
    out += ["### Shift", "", _distribution(Counter(d.shift for d in drivers)), ""]
    out += ["### Depot", "", _distribution(Counter(d.depot for d in drivers)), ""]

    per_course = defaultdict(list)
    for s in sessions:
        per_course[s.course_id].append(s)
    rows = []
    for c in courses:
        seats = sum(s.capacity for s in per_course[c.id])
        rows.append([c.code, c.name, "mandatory" if c.is_mandatory else "optional", targets[c.id],
                     len(per_course[c.id]), seats, _pct(seats, targets[c.id])])
    out += ["### Sessions per course (planned capacity vs target)", "",
            _md_table(["Code", "Course", "Type", "Target", "Sessions", "Planned seats", "Seats / target"], rows), ""]

    per_trainer = Counter(s.trainer_id for s in sessions)
    weekly = Counter((s.trainer_id, week_start(s.start_time.date())) for s in sessions)
    rows = []
    for t in trainers:
        peak = max((n for (tid, _), n in weekly.items() if tid == t.id), default=0)
        rows.append([t.id, t.name, per_trainer[t.id], peak, t.max_sessions_per_week])
    out += ["### Sessions per trainer", "",
            _md_table(["ID", "Trainer", "Sessions", "Busiest week", "Weekly max"], rows), ""]

    window_end = SIM_START + timedelta(days=BOOKING_WINDOW_DAYS)
    first = [s for s in sessions if s.start_time < window_end]
    seats = sum(s.capacity for s in first)
    taken = sum(booked[s.id] for s in first)
    out += ["### Fill rate at seed", "",
            f"Enrollments exist only for sessions in the first {BOOKING_WINDOW_DAYS} days: "
            f"{taken} booked of {seats} seats in {len(first)} sessions ({_pct(taken, seats)})."]
    return "\n".join(out)


# --- Section 2: full-year dry run -----------------------------------------------------------
def dry_run_data(db: Session) -> dict:
    """Collect everything the checks and tables need after the year has been played out."""
    courses = {c.id: c for c in db.scalars(select(Course))}
    targets = {t.course_id: t.target_completions for t in db.scalars(select(TrainingTarget))}
    drivers = {d.id: d for d in db.scalars(select(Driver))}
    rows = db.execute(
        select(Enrollment.status, Enrollment.driver_id, TrainingSession.course_id, TrainingSession.start_time,
               TrainingSession.trainer_id, TrainingSession.status)
        .join(TrainingSession, Enrollment.session_id == TrainingSession.id)
    ).all()
    return {"courses": courses, "targets": targets, "drivers": drivers, "rows": rows,
            "sessions": list(db.scalars(select(TrainingSession)))}


def dry_run_section(data: dict) -> str:
    courses, targets, drivers, rows = data["courses"], data["targets"], data["drivers"], data["rows"]
    attended = Counter(cid for status, _, cid, *_ in rows if status == "attended")

    out = ["## 2. Full-year dry run (seed → 2026-12-31)", "", "### Final attended vs target", ""]
    table = []
    for cid, c in courses.items():
        table.append([c.code, c.name, targets[cid], attended[cid], _pct(attended[cid], targets[cid])])
    out += [_md_table(["Code", "Course", "Target", "Attended", "% of target"], table), ""]

    by_shift = defaultdict(lambda: [0, 0])
    by_month = defaultdict(lambda: [0, 0])
    for status, driver_id, _, start, *_ in rows:
        if status not in ("attended", "no_show"):
            continue
        for bucket in (by_shift[drivers[driver_id].shift], by_month[start.month]):
            bucket[0] += status == "no_show"
            bucket[1] += 1
    out += ["### No-show rate by shift", "",
            _md_table(["Shift", "No-shows", "Seats used", "Rate"],
                      [[k, v[0], v[1], _pct(*v)] for k, v in sorted(by_shift.items())]), ""]
    out += ["### No-show rate by month (session month)", "",
            _md_table(["Month", "No-shows", "Seats used", "Rate"],
                      [[f"2026-{m:02d}", v[0], v[1], _pct(*v)] for m, v in sorted(by_month.items())]), ""]
    return "\n".join(out)


# --- Section 3: scenario checks -------------------------------------------------------------
def scenario_checks(db: Session, data: dict, seed: int, determinism: tuple[str, str]) -> list[tuple[str, bool, str]]:
    """Each check is (name, passed, evidence)."""
    courses, targets, drivers, rows = data["courses"], data["targets"], data["drivers"], data["rows"]
    code_to_id = {c.code: cid for cid, c in courses.items()}
    attended = Counter(cid for status, _, cid, *_ in rows if status == "attended")
    checks: list[tuple[str, bool, str]] = []

    # 1. Night-shift mismatch
    def_id = code_to_id[scenarios.NIGHT_MISMATCH_COURSE]
    reached = {d for status, d, cid, *_ in rows if cid == def_id and status == "attended"}
    shift_pop = Counter(d.shift for d in drivers.values() if d.is_active)
    shift_done = Counter(drivers[d].shift for d in reached)
    rate = {s: shift_done[s] / shift_pop[s] for s in shift_pop}
    missing_night = shift_pop["night"] - shift_done["night"]
    missing_all = sum(shift_pop.values()) - len(reached)
    night_share_pop = shift_pop["night"] / sum(shift_pop.values())
    night_share_gap = missing_night / missing_all if missing_all else 0
    behind = attended[def_id] < targets[def_id]
    checks.append((
        "Night-shift mismatch: Defensive Driving finishes below target",
        behind, f"attended {attended[def_id]} of target {targets[def_id]} ({_pct(attended[def_id], targets[def_id])})"))
    checks.append((
        "Night-shift mismatch: gap is concentrated on night shift",
        rate["night"] < rate["day"] - 0.10 and night_share_gap > night_share_pop,
        f"completion night {rate['night']:.0%} vs day {rate['day']:.0%}; night drivers are "
        f"{night_share_pop:.0%} of the fleet but {night_share_gap:.0%} of drivers who never completed it"))

    # 2. Trainer leave
    event = next(e for e in scenarios.SCENARIO_EVENTS if e.kind == "trainer_leave")
    leave_from = datetime.combine(event.date, time.min)
    leave_to = leave_from + timedelta(weeks=event.params["weeks"])
    trainer_id = event.params["trainer_id"]
    in_window = [s for s in data["sessions"] if s.trainer_id == trainer_id and leave_from <= s.start_time < leave_to]
    applied = db.scalars(select(AuditLog).where(AuditLog.action == "scenario_event_applied")).all()
    cancelled_ok = bool(in_window) and all(s.status == "cancelled" for s in in_window)
    once = len([a for a in applied if a.details.get("key") == event.key]) == 1
    checks.append((
        "Trainer leave: sessions in the leave window are cancelled, event logged exactly once",
        cancelled_ok and once,
        f"{len(in_window)} sessions of trainer {trainer_id} in the window, "
        f"{sum(s.status == 'cancelled' for s in in_window)} cancelled; audit rows for the event: "
        f"{len([a for a in applied if a.details.get('key') == event.key])}"))

    fa_id = code_to_id["FA"]
    fa_in = sum(1 for st, _, cid, start, *_ in rows if cid == fa_id and st == "attended" and leave_from <= start < leave_to)
    fa_out = sum(1 for st, _, cid, start, *_ in rows if cid == fa_id and st == "attended" and not leave_from <= start < leave_to)
    weeks_in = event.params["weeks"]
    weeks_out = (365 / 7) - weeks_in
    ratio = (fa_in / weeks_in) / (fa_out / weeks_out)
    checks.append((
        "Trainer leave: First Aid attendance dips during the leave window",
        ratio < 0.75,
        f"{fa_in / weeks_in:.1f} attended/week inside the window vs {fa_out / weeks_out:.1f} outside (ratio {ratio:.2f})"))

    # 3. Seasonal dips. One seed only has ~110 Ramadan seats (about +/-4 points of noise), so the
    #    pass/fail test pools this seed with a few extra seeds; the seed's own numbers are shown too.
    def season_counts(seed_rows) -> dict[str, list[int]]:
        out = {"base": [0, 0], "ramadan": [0, 0], "summer": [0, 0]}
        for st, _, _, start, *_ in seed_rows:
            if st not in ("attended", "no_show"):
                continue
            d = start.date()
            key = "ramadan" if is_ramadan(d) else "summer" if is_summer_peak(d) else "base"
            out[key][0] += st == "no_show"
            out[key][1] += 1
        return out

    own = season_counts(rows)
    pooled = {k: list(v) for k, v in own.items()}
    for extra_seed in range(seed + 1, seed + 1 + POOLED_EXTRA_SEEDS):
        extra_engine = _fresh_engine()
        reseed(extra_engine, extra_seed)
        with Session(extra_engine) as extra_db:
            sim_engine.advance(extra_db, 400)
            for key, (a, b) in season_counts(dry_run_data(extra_db)["rows"]).items():
                pooled[key][0] += a
                pooled[key][1] += b

    def r(v: list[int]) -> float:
        return v[0] / v[1]

    checks.append((
        "Seasonal dips: no-show rate is higher in Ramadan and in the summer peak",
        r(pooled["ramadan"]) > r(pooled["base"]) + 0.04 and r(pooled["summer"]) > r(pooled["base"]) + 0.04
        and r(own["ramadan"]) > r(own["base"]) and r(own["summer"]) > r(own["base"]),
        f"seed {seed}: baseline {r(own['base']):.1%} (n={own['base'][1]}), Ramadan {r(own['ramadan']):.1%} "
        f"(n={own['ramadan'][1]}), summer {r(own['summer']):.1%} (n={own['summer'][1]}). "
        f"Pooled over {POOLED_EXTRA_SEEDS + 1} seeds: baseline {r(pooled['base']):.1%}, Ramadan {r(pooled['ramadan']):.1%} "
        f"(n={pooled['ramadan'][1]}), summer {r(pooled['summer']):.1%} (n={pooled['summer'][1]})"))

    # 4. Healthy control
    ctl = code_to_id[scenarios.CONTROL_COURSE]
    checks.append((
        "Healthy control: Road Safety Refresher finishes at least 5% above target",
        attended[ctl] >= 1.05 * targets[ctl], f"attended {attended[ctl]} of target {targets[ctl]} ({_pct(attended[ctl], targets[ctl])})"))

    # Overall: the data really contains shortfalls
    short = [courses[c].code for c in courses if c != ctl and attended[c] < 0.95 * targets[c]]
    checks.append((
        "Overall: at least 3 other courses end more than 5% below target",
        len(short) >= 3, f"{len(short)} courses: {', '.join(short)}"))

    checks.append((
        "Determinism: the same seed gives identical data",
        determinism[0] == determinism[1], f"fingerprints {determinism[0]} and {determinism[1]}"))
    return checks


def checks_section(checks: list[tuple[str, bool, str]]) -> str:
    rows = [["PASS" if ok else "FAIL", name, evidence] for name, ok, evidence in checks]
    return "\n".join(["## 3. Scenario checks", "", _md_table(["Result", "Check", "Evidence"], rows), ""])


# --- Section 4: assumptions -----------------------------------------------------------------
def assumptions_section() -> str:
    e = sim_engine
    return f"""## 4. Assumptions

**Calendar and rules** (`app/simulator/rules.py`)
- Simulated year 2026, clock starts 2026-01-01 00:00 and stops at 2026-12-31 23:59.
- Shifts: day 06:00-14:00, night 22:00-06:00, rotating alternates weekly (even ISO week = day, odd = night).
- Sessions start at 09:00, 14:00 or 18:00, Sunday to Thursday, at most 4 hours long.
- Rest rule: a driver cannot attend a session that overlaps their shift or starts less than {REST_HOURS} hours after it ends.
- Because that rule alone would block every slot for day-shift drivers on working days, each driver gets two fixed rest days a week (derived from their id). On a rest day there is no shift to clash with.
- Trainers: no double booking and no more than `max_sessions_per_week` sessions in a Sunday-Saturday week.
- Drivers are booked into sessions starting within the next {BOOKING_WINDOW_DAYS} days.
- Ramadan {RAMADAN_START} to {RAMADAN_END} and summer peak {SUMMER_PEAK_START} to {SUMMER_PEAK_END} are approximate.

**Probabilities** (`app/simulator/engine.py`, `scenarios.py`)
- Attendance: base {e.BASE_ATTENDANCE:.2f}; minus {e.NIGHT_MORNING_PENALTY:.2f} for a night worker in a morning session; minus {scenarios.RAMADAN_ATTENDANCE_PENALTY:.2f} in Ramadan; minus {scenarios.SUMMER_ATTENDANCE_PENALTY:.2f} in the summer peak; 0 if unavailable.
- Sick leave: {e.SICK_DAILY_PROBABILITY:.1%} per active driver per day, lasting 1-{e.SICK_MAX_DAYS} days. Bookings in the sick window are cancelled; a session on the day itself becomes a no-show.
- Booking fill: each session aims for a random share of capacity between {scenarios.DEFAULT_DEMAND - 0.15:.2f} and {scenarios.DEFAULT_DEMAND:.2f} (control course: {scenarios.DEMAND_BY_COURSE[scenarios.CONTROL_COURSE] - 0.15:.2f} to {min(1.0, scenarios.DEMAND_BY_COURSE[scenarios.CONTROL_COURSE]):.2f}).
- Every random decision uses its own RNG seeded from (seed, decision, ids) with SHA-256, so results do not depend on how days are grouped into `advance` calls.

**Data**
- 300 drivers (about 50% day, 35% night, 15% rotating), 5% hired during 2026; 10 trainers; 8 courses (4 mandatory).
- Mandatory targets are 85-92% of active drivers; optional targets 8-35%. Planned seats are 110-130% of target. The RSR control is the exception: target 74-78% of active drivers and 145% planned seats, so that it clearly finishes above target.
- Annual leave of 2-4 weeks per driver (65% starting mid-June to mid-August), some second blocks, and 20 short known absences.
"""


def build_report(seed: int) -> tuple[str, list[tuple[str, bool, str]]]:
    e1, e2 = _fresh_engine(), _fresh_engine()
    reseed(e1, seed)
    reseed(e2, seed)
    with Session(e1) as db1, Session(e2) as db2:
        before = fingerprint(db1)
        same = fingerprint(db2)
        seed_md = seed_section(db1)
        sim_engine.advance(db1, 400)  # runs to year end
        data = dry_run_data(db1)
        checks = scenario_checks(db1, data, seed, (before, same))
        body = "\n\n".join([
            "# Synthetic data validation report",
            f"Generated by `python -m app.simulator.report --seed {seed}`. "
            "The dry run uses a temporary in-memory copy built from the same seed; `training.db` is not touched.",
            seed_md, dry_run_section(data), checks_section(checks), assumptions_section(),
        ])
    return body, checks


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    body, checks = build_report(args.seed)
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(body, encoding="utf-8")
    for name, ok, evidence in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {name}\n      {evidence}")
    print(f"\nWrote {REPORT_PATH}")


if __name__ == "__main__":
    main()
