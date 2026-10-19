"""The simulator: plays the year forward one day at a time.

Daily order (see ``_process_day``):
1. apply scenario events dated today      4. attendance for sessions that finished
2. activate newly hired drivers           5. book drivers into the next 21 days of sessions
3. roll sick days                         6. (in ``advance``) move the clock; after a Saturday also ``weekly_close`` (forecast snapshots + alerts)

Every random choice uses ``rng_for(seed, ...)`` with the ids involved, so the result does not
depend on how the days are grouped into ``advance`` calls.
"""
from collections import defaultdict
from datetime import date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.live.bus import bus
from app.models import AuditLog, Course, Driver, DriverUnavailability, Enrollment, Trainer, TrainingSession
from app.schemas.sim import AdvanceSummary, DayBreakdown
from app.services.audit import log_event
from app.services.sim_clock import get_sim_now, set_sim_now
from app.services.tracking_jobs import weekly_close
from app.simulator import scenarios
from app.simulator.rng import rng_for
from app.simulator.rules import (
    BOOKING_WINDOW_DAYS,
    SHIFT_NIGHT,
    SIM_END,
    effective_shift,
    is_driver_free,
    is_ramadan,
    is_summer_peak,
)

# --- Attendance model -------------------------------------------------------------------
BASE_ATTENDANCE = 0.88
NIGHT_MORNING_PENALTY = 0.35  # night worker booked into a morning (before 12:00) session
MORNING_BEFORE_HOUR = 12
SICK_DAILY_PROBABILITY = 0.004  # per active driver per day
SICK_MAX_DAYS = 3
SATURDAY = 5  # Python weekday(); weeks run Sunday to Saturday


def attendance_probability(driver: Driver, session: TrainingSession, unavailable: bool) -> float:
    """Chance that a booked driver actually turns up.

    Start from BASE_ATTENDANCE, then subtract:
    - NIGHT_MORNING_PENALTY if the driver is on a night shift that week and the session is in the morning;
    - the Ramadan penalty and/or the summer-peak penalty (scenario 3).
    A driver who is unavailable (e.g. fell sick that morning) never attends.
    """
    if unavailable:
        return 0.0
    day = session.start_time.date()
    p = BASE_ATTENDANCE
    if effective_shift(driver, day) == SHIFT_NIGHT and session.start_time.hour < MORNING_BEFORE_HOUR:
        p -= NIGHT_MORNING_PENALTY
    if is_ramadan(day):
        p -= scenarios.RAMADAN_ATTENDANCE_PENALTY
    if is_summer_peak(day):
        p -= scenarios.SUMMER_ATTENDANCE_PENALTY
    return max(0.0, min(1.0, p))


def get_seed(db: Session) -> int:
    """The seed used to generate this database (stored in the seed_created audit row)."""
    row = db.scalars(select(AuditLog).where(AuditLog.action == "seed_created").order_by(AuditLog.id.desc())).first()
    if row is None:
        raise RuntimeError("No seed_created audit row: the database has not been seeded")
    return int(row.details["seed"])


def _overlaps(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    return a_start < b_end and a_end > b_start


# --- Booking (shared with the seed script) ----------------------------------------------
def book_window(db: Session, seed: int, now: datetime) -> int:
    """Book eligible drivers into scheduled sessions starting in [now, now + 21 days). Returns new bookings.

    An eligible driver for a course is active, has not attended that course, is not already
    booked in a future session of it, has no overlapping booking, and passes ``is_driver_free``.
    Each session aims for a random share of its capacity (see scenarios.DEMAND_*), so seats are
    not always full. Does not commit.
    """
    window_end = now + timedelta(days=BOOKING_WINDOW_DAYS)
    sessions = list(
        db.scalars(
            select(TrainingSession)
            .where(
                TrainingSession.status == "scheduled",
                TrainingSession.start_time >= now,
                TrainingSession.start_time < window_end,
            )
            .order_by(TrainingSession.start_time, TrainingSession.id)
        )
    )
    if not sessions:
        return 0

    course_codes = {c.id: c.code for c in db.scalars(select(Course))}
    drivers = list(db.scalars(select(Driver).where(Driver.is_active.is_(True)).order_by(Driver.id)))

    unavailability: dict[int, list[DriverUnavailability]] = defaultdict(list)
    for row in db.scalars(select(DriverUnavailability).where(DriverUnavailability.end_time > now)):
        unavailability[row.driver_id].append(row)

    attended = {
        (course_id, driver_id)
        for course_id, driver_id in db.execute(
            select(TrainingSession.course_id, Enrollment.driver_id)
            .join(Enrollment, Enrollment.session_id == TrainingSession.id)
            .where(Enrollment.status == "attended")
        )
    }

    # Future bookings: per driver (for overlap checks) and per course (one booking per course).
    booked_by_driver: dict[int, list[tuple[datetime, datetime]]] = defaultdict(list)
    booked_for_course: dict[int, set[int]] = defaultdict(set)
    seats_taken: dict[int, int] = defaultdict(int)
    for session_id, driver_id, course_id, start, end in db.execute(
        select(
            Enrollment.session_id, Enrollment.driver_id, TrainingSession.course_id,
            TrainingSession.start_time, TrainingSession.end_time,
        )
        .join(TrainingSession, Enrollment.session_id == TrainingSession.id)
        .where(Enrollment.status == "booked", TrainingSession.status == "scheduled")
    ):
        booked_by_driver[driver_id].append((start, end))
        booked_for_course[course_id].add(driver_id)
        seats_taken[session_id] += 1

    window_ids = [s.id for s in sessions]
    existing_pairs = set(
        db.execute(select(Enrollment.session_id, Enrollment.driver_id).where(Enrollment.session_id.in_(window_ids)))
    )

    new_bookings = 0
    for session in sessions:
        code = course_codes[session.course_id]
        demand = scenarios.DEMAND_BY_COURSE.get(code, scenarios.DEFAULT_DEMAND)
        share = min(1.0, rng_for(seed, "fill", session.id).uniform(demand - 0.15, demand))
        wanted = min(session.capacity, round(session.capacity * share))
        missing = wanted - seats_taken[session.id]
        if missing <= 0:
            continue

        order = list(drivers)
        rng_for(seed, "book", now.isoformat(), session.id).shuffle(order)
        for driver in order:
            if missing <= 0:
                break
            if (session.course_id, driver.id) in attended:
                continue
            if driver.id in booked_for_course[session.course_id]:
                continue
            if (session.id, driver.id) in existing_pairs:
                continue
            if any(_overlaps(session.start_time, session.end_time, s, e) for s, e in booked_by_driver[driver.id]):
                continue
            free, _reason = is_driver_free(driver, session.start_time, session.end_time, unavailability[driver.id])
            if not free:
                continue

            db.add(Enrollment(session_id=session.id, driver_id=driver.id, status="booked"))
            booked_by_driver[driver.id].append((session.start_time, session.end_time))
            booked_for_course[session.course_id].add(driver.id)
            existing_pairs.add((session.id, driver.id))
            seats_taken[session.id] += 1
            missing -= 1
            new_bookings += 1

    db.flush()
    return new_bookings


# --- Scenario events --------------------------------------------------------------------
def _apply_trainer_leave(db: Session, event: scenarios.ScenarioEvent, now: datetime) -> dict:
    """Cancel the trainer's scheduled sessions in the leave window and their bookings."""
    trainer_id = event.params["trainer_id"]
    window_start = datetime.combine(event.date, time.min)
    window_end = window_start + timedelta(weeks=event.params["weeks"])
    sessions = list(
        db.scalars(
            select(TrainingSession).where(
                TrainingSession.trainer_id == trainer_id,
                TrainingSession.status == "scheduled",
                TrainingSession.start_time >= window_start,
                TrainingSession.start_time < window_end,
            )
        )
    )
    enrollments_cancelled = 0
    for session in sessions:
        session.status = "cancelled"
        for enrollment in db.scalars(
            select(Enrollment).where(Enrollment.session_id == session.id, Enrollment.status == "booked")
        ):
            enrollment.status = "cancelled"
            enrollments_cancelled += 1
    trainer = db.get(Trainer, trainer_id)
    return {
        "trainer_id": trainer_id,
        "trainer_name": trainer.name if trainer else None,
        "leave_from": window_start.isoformat(),
        "leave_until": window_end.isoformat(),
        "sessions_cancelled": [s.id for s in sessions],
        "enrollments_cancelled": enrollments_cancelled,
    }


EVENT_HANDLERS = {"trainer_leave": _apply_trainer_leave}


def _apply_scenario_events(db: Session, day: date, day_stats: DayBreakdown) -> None:
    """Apply every event dated on or before ``day`` that has not been applied yet (each runs once)."""
    applied_keys = {
        row.details.get("key") for row in db.scalars(select(AuditLog).where(AuditLog.action == "scenario_event_applied"))
    }
    for event in scenarios.SCENARIO_EVENTS:
        if event.date > day or event.key in applied_keys:
            continue
        result = EVENT_HANDLERS[event.kind](db, event, datetime.combine(day, time.min))
        log_event(
            db, datetime.combine(day, time.min), "system", "scenario_event_applied", "scenario", None,
            {"key": event.key, "kind": event.kind, "description": event.description, **result},
        )
        day_stats.scenario_events.append(event.key)
        day_stats.sessions_cancelled += len(result["sessions_cancelled"])
        day_stats.enrollments_cancelled += result["enrollments_cancelled"]


# --- One simulated day ------------------------------------------------------------------
def _process_day(db: Session, seed: int, day: date) -> DayBreakdown:
    stats = DayBreakdown(day=day)
    day_start = datetime.combine(day, time.min)
    next_start = day_start + timedelta(days=1)

    # 1. Scenario events
    _apply_scenario_events(db, day, stats)

    # 2. Hires reaching their start date
    for driver in db.scalars(select(Driver).where(Driver.is_active.is_(False), Driver.hire_date <= day)):
        driver.is_active = True
        stats.drivers_activated += 1
    db.flush()

    drivers = {d.id: d for d in db.scalars(select(Driver).order_by(Driver.id))}

    # 3. Sick days. Sessions starting today keep their booking (the driver simply no-shows);
    #    later sessions inside the sick window are cancelled.
    on_leave_today = set(
        db.scalars(
            select(DriverUnavailability.driver_id).where(
                DriverUnavailability.start_time < next_start, DriverUnavailability.end_time > day_start
            )
        )
    )
    for driver in drivers.values():
        if not driver.is_active or driver.id in on_leave_today:
            continue
        rng = rng_for(seed, "sick", day.isoformat(), driver.id)
        if rng.random() >= SICK_DAILY_PROBABILITY:
            continue
        sick_days = rng.randint(1, SICK_MAX_DAYS)
        sick_end = day_start + timedelta(days=sick_days)
        db.add(DriverUnavailability(driver_id=driver.id, start_time=day_start, end_time=sick_end, reason="sick leave"))
        stats.sick_events += 1
        for enrollment in db.scalars(
            select(Enrollment)
            .join(TrainingSession, Enrollment.session_id == TrainingSession.id)
            .where(
                Enrollment.driver_id == driver.id,
                Enrollment.status == "booked",
                TrainingSession.status == "scheduled",
                TrainingSession.start_time >= next_start,
                TrainingSession.start_time < sick_end,
            )
        ):
            enrollment.status = "cancelled"
            stats.enrollments_cancelled += 1
    db.flush()

    # 4. Attendance for sessions that have finished
    unavailability: dict[int, list[DriverUnavailability]] = defaultdict(list)
    for row in db.scalars(select(DriverUnavailability).where(DriverUnavailability.end_time > day_start)):
        unavailability[row.driver_id].append(row)

    finished = db.scalars(
        select(TrainingSession)
        .where(TrainingSession.status == "scheduled", TrainingSession.end_time < next_start)
        .order_by(TrainingSession.start_time, TrainingSession.id)
    )
    for session in list(finished):
        booked = db.scalars(
            select(Enrollment)
            .where(Enrollment.session_id == session.id, Enrollment.status == "booked")
            .order_by(Enrollment.id)
        )
        for enrollment in booked:
            driver = drivers[enrollment.driver_id]
            unavailable = any(
                _overlaps(session.start_time, session.end_time, row.start_time, row.end_time)
                for row in unavailability[driver.id]
            )
            p = attendance_probability(driver, session, unavailable)
            if rng_for(seed, "attend", session.id, driver.id).random() < p:
                enrollment.status = "attended"
                stats.attended += 1
            else:
                enrollment.status = "no_show"
                stats.no_shows += 1
        session.status = "completed"
        stats.sessions_completed += 1
    db.flush()

    # 5. Book drivers into sessions starting in the next 21 days (counted from tomorrow 00:00)
    stats.new_bookings = book_window(db, seed, next_start)
    return stats


def advance(db: Session, days: int) -> AdvanceSummary:
    """Advance the simulated clock by up to ``days`` days, one day at a time. Commits after each day.

    Stops at SIM_END (2026-12-31 23:59); asking for more days than remain just runs to the end.
    """
    seed = get_seed(db)
    start = get_sim_now(db)
    now = start
    day_rows: list[DayBreakdown] = []

    for _ in range(days):
        if now >= SIM_END:
            break
        day = now.date()
        day_rows.append(_process_day(db, seed, day))
        now = min(datetime.combine(day + timedelta(days=1), time.min), SIM_END)
        set_sim_now(db, now)
        if day.weekday() == SATURDAY:  # the week just closed: snapshot forecasts and sync alerts
            weekly_close(db)
        db.commit()

    def total(field: str) -> int:
        return sum(getattr(d, field) for d in day_rows)

    summary = AdvanceSummary(
        from_time=start,
        to_time=now,
        days_advanced=len(day_rows),
        hit_year_end=now >= SIM_END,
        sessions_completed=total("sessions_completed"),
        attended=total("attended"),
        no_shows=total("no_shows"),
        new_bookings=total("new_bookings"),
        enrollments_cancelled=total("enrollments_cancelled"),
        sessions_cancelled=total("sessions_cancelled"),
        sick_events=total("sick_events"),
        drivers_activated=total("drivers_activated"),
        scenario_events_applied=[k for d in day_rows for k in d.scenario_events],
        days=day_rows,
    )
    log_event(db, now, "system", "sim_advanced", "sim_state", 1, summary.model_dump(mode="json"))
    db.commit()
    if summary.days_advanced:
        _publish_advance(db, start, now, summary)
    return summary


def _publish_advance(db: Session, start: datetime, now: datetime, summary: AdvanceSummary) -> None:
    """One set of live events per advance call (not per simulated day), published after the final commit."""
    finished = list(db.scalars(select(TrainingSession.id).where(
        TrainingSession.status == "completed", TrainingSession.end_time >= start, TrainingSession.end_time < now)))
    bus.publish("sim.advanced", {"sim_time": now.isoformat(), "days_advanced": summary.days_advanced})
    if summary.attended or summary.no_shows:
        bus.publish("attendance.recorded", {"session_ids": finished[:200], "count": summary.attended + summary.no_shows})
    bus.publish("sessions.changed", {"session_ids": finished[:200]})
