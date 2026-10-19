"""Does a valid fix exist for an alert at all? A deterministic check that reuses the checker's own rule functions. Read-only.

Run from the backend folder:   python -m scripts.feasibility_check <alert_id> [--days 28]

It answers four questions:
1. Who is in the eligible pool (not yet trained, not yet booked), by shift?
2. For each candidate start time (every hour 06:00-20:00 for the next N days), how many pool drivers pass the booking rules,
   and which trainers pass the new-session rules?
3. Which start hours work for each shift group?
4. Do any existing sessions with free seats accept pool drivers?
Uses DATABASE_URL (default backend/training.db).
"""
import argparse
import sys
from collections import Counter, defaultdict
from datetime import datetime, time, timedelta

from sqlalchemy import select

from app.database import SessionLocal
from app.models import Alert, Course, Driver, Enrollment, Trainer, TrainingSession
from app.schemas.plan import AddSession
from app.services.constraints import SessionInfo, driver_can_book, load_snapshot_for_ids, trainer_can_run
from app.services.sim_clock import get_sim_now
from app.simulator.rules import SESSION_START_HOURS, SESSION_WEEKDAYS, SIM_YEAR, session_end

HOURS = range(6, 21)  # candidate start hours 06:00 to 20:00


def pool_for(db, course_id: int) -> list[Driver]:
    """Same pool as the find_eligible_drivers tool: active, not attended and not booked for this course this year."""
    year = (datetime(SIM_YEAR, 1, 1), datetime(SIM_YEAR + 1, 1, 1))
    attended = select(Enrollment.driver_id).join(TrainingSession, TrainingSession.id == Enrollment.session_id).where(
        Enrollment.status == "attended", TrainingSession.course_id == course_id,
        TrainingSession.start_time >= year[0], TrainingSession.start_time < year[1])
    booked = select(Enrollment.driver_id).join(TrainingSession, TrainingSession.id == Enrollment.session_id).where(
        Enrollment.status == "booked", TrainingSession.course_id == course_id, TrainingSession.status == "scheduled")
    return list(db.scalars(select(Driver).where(Driver.is_active.is_(True), Driver.id.not_in(attended), Driver.id.not_in(booked)).order_by(Driver.id)))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("alert_id", type=int)
    parser.add_argument("--days", type=int, default=28)
    args = parser.parse_args()
    with SessionLocal() as db:
        alert = db.get(Alert, args.alert_id)
        if alert is None:
            print(f"No alert {args.alert_id}.")
            return 1
        course = db.get(Course, alert.course_id)
        now = get_sim_now(db)
        print(f"Alert {alert.id}: {course.name} ({alert.shortfall_type}), simulated now {now:%a %d %b %Y %H:%M}")
        print(f"Course length {course.duration_hours}h, default capacity {course.default_capacity}; window: next {args.days} days\n")

        pool = pool_for(db, course.id)
        by_shift = Counter(d.shift for d in pool)
        print(f"1. Eligible unenrolled pool: {len(pool)} drivers")
        for shift, n in by_shift.most_common():
            print(f"   {shift:10s} {n:4d}  ({100 * n / len(pool):.0f}%)" if pool else "")
        trainers = list(db.scalars(select(Trainer).order_by(Trainer.id)))
        snap = load_snapshot_for_ids(db, course.id, now, {d.id for d in pool}, set(), {t.id for t in trainers})

        # 2 and 3: every candidate start time
        windows = []  # (start, count, count_by_shift, free_trainers)
        for offset in range(1, args.days + 1):
            day = now.date() + timedelta(days=offset)
            for hour in HOURS:
                start = datetime.combine(day, time(hour))
                end = session_end(start, course.duration_hours)
                info = SessionInfo("new:1", course.id, trainers[0].id, start, end, course.default_capacity)
                ok = [d for d in pool if not driver_can_book(snap, d, info)]
                free = [t.id for t in trainers if not trainer_can_run(snap, AddSession(
                    type="add_session", temp_ref="new:1", course_id=course.id, trainer_id=t.id, start=start, end=end,
                    location="Training Room", capacity=course.default_capacity))]
                windows.append((start, len(ok), Counter(d.shift for d in ok), free))

        usable = [w for w in windows if w[1] > 0 and w[3]]
        print(f"\n2. Candidate start times checked: {len(windows)}; with at least one driver AND a free trainer: {len(usable)}")
        print("   Same, restricted to the ERP slot grid (Sun-Thu at "
              f"{', '.join(f'{h:02d}:00' for h in SESSION_START_HOURS)}): "
              f"{len([w for w in usable if w[0].weekday() in SESSION_WEEKDAYS and w[0].hour in SESSION_START_HOURS])}")

        print("\n3. Which start hours work, per shift group (share of days in the window with >=1 passing driver of that shift):")
        days_with = defaultdict(lambda: defaultdict(int))
        totals = defaultdict(int)
        for start, _n, shifts, free in windows:
            if not free:
                continue
            totals[start.hour] += 1
            for shift in by_shift:
                if shifts.get(shift):
                    days_with[shift][start.hour] += 1
        header = "   hour  " + "".join(f"{s[:9]:>10s}" for s in by_shift)
        print(header)
        for hour in HOURS:
            if not totals[hour]:
                print(f"   {hour:02d}:00  (no trainer is free at this hour on any day)")
                continue
            print(f"   {hour:02d}:00  " + "".join(f"{days_with[s][hour]:>4d}/{totals[hour]:<5d}" for s in by_shift))
        # Best windows
        print("\n   Ten windows with the most passing drivers (and a free trainer):")
        for start, n, shifts, free in sorted(usable, key=lambda w: (-w[1], w[0]))[:10]:
            print(f"   {start:%a %d %b %H:%M}: {n} drivers {dict(shifts)}; free trainers {free}")

        # 4: existing sessions
        print("\n4. Existing upcoming sessions with free seats:")
        rows = list(db.scalars(select(TrainingSession).where(
            TrainingSession.course_id == course.id, TrainingSession.status == "scheduled", TrainingSession.start_time > now
        ).order_by(TrainingSession.start_time)))
        existing_ok = 0
        for row in rows[:12]:
            booked = len(snap.roster.get(row.id, ())) if row.id in snap.roster else db.scalar(
                select(Enrollment.id).where(Enrollment.session_id == row.id).limit(1)) and 0
            info = SessionInfo(row.id, course.id, row.trainer_id, row.start_time, row.end_time, row.capacity)
            n = sum(1 for d in pool if not driver_can_book(snap, d, info))
            existing_ok += n > 0
            print(f"   session {row.id}: {row.start_time:%a %d %b %H:%M}, capacity {row.capacity}: {n} pool drivers pass")
        if not rows:
            print("   none")

        print("\n   Note: there is no room table, and the checker has no room rule, so 'room free' is always true.")
        exists = bool(usable) or existing_ok > 0
        print(f"\nA valid plan exists for this alert: {'YES' if exists else 'NO'} "
              f"({len(usable)} usable new-session windows, {existing_ok} existing sessions that take drivers)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
