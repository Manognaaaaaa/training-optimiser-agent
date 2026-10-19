"""Which drivers, trainers and times can actually work for a course? Deterministic and read-only.

Everything here asks the constraint checker's own rule functions (``driver_can_book``, ``trainer_can_run``), so a
window or driver reported as "works" can never be refused by the checker later. The agent tools, the greedy baseline,
the alert context and the "no valid plan exists" short-circuit all use this one module.

Planner policy applies on top (not a checker rule): new sessions must start at least ``planner_min_lead_hours`` from now.
"""
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Course, Driver, Enrollment, Trainer, TrainingSession
from app.schemas.plan import AddSession
from app.services.constraints import SessionInfo, Snapshot, driver_can_book, load_snapshot_for_ids, trainer_can_run
from app.services.planner_policy import earliest_new_session_start
from app.simulator.rules import SESSION_START_HOURS, SESSION_WEEKDAYS, SIM_YEAR, session_end

ALL_START_HOURS = tuple(range(6, 21))  # 06:00 to 20:00, for "which hours work at all" questions
DEFAULT_LOCATION = "Training Room"
TEMP_KEY = "new:1"


@dataclass
class Window:
    """One candidate start time for a new session, and who could use it."""

    start: datetime
    end: datetime
    available: list[int]  # ids of pool drivers who pass every booking rule
    by_shift: dict[str, int]
    free_trainers: list[int]  # ids of trainers who pass every new-session rule

    @property
    def count(self) -> int:
        return len(self.available)

    def as_dict(self, limit_trainers: int = 5) -> dict:
        return {
            "start": self.start.isoformat(timespec="minutes"), "end": self.end.isoformat(timespec="minutes"),
            "available_driver_count": self.count, "available_by_shift": dict(sorted(self.by_shift.items())),
            "qualified_trainers_free": self.free_trainers[:limit_trainers],
            "room_free": True,  # the data has no rooms, so a room is never the constraint
        }


def pool_drivers(db: Session, course_id: int) -> list[Driver]:
    """Active drivers who have not attended this course this year and are not booked into an upcoming session of it."""
    year = (datetime(SIM_YEAR, 1, 1), datetime(SIM_YEAR + 1, 1, 1))
    attended = select(Enrollment.driver_id).join(TrainingSession, TrainingSession.id == Enrollment.session_id).where(
        Enrollment.status == "attended", TrainingSession.course_id == course_id,
        TrainingSession.start_time >= year[0], TrainingSession.start_time < year[1])
    booked = select(Enrollment.driver_id).join(TrainingSession, TrainingSession.id == Enrollment.session_id).where(
        Enrollment.status == "booked", TrainingSession.course_id == course_id, TrainingSession.status == "scheduled")
    return list(db.scalars(
        select(Driver).where(Driver.is_active.is_(True), Driver.id.not_in(attended), Driver.id.not_in(booked)).order_by(Driver.id)))


def interleave_by_shift(drivers: list[Driver]) -> list[Driver]:
    """Mix shifts (day, night, rotating, day, night, ...) so a short list is not all one group."""
    groups: dict[str, list[Driver]] = {}
    for d in drivers:
        groups.setdefault(d.shift, []).append(d)
    mixed: list[Driver] = []
    queues = [groups[k] for k in sorted(groups)]
    while any(queues):
        for q in queues:
            if q:
                mixed.append(q.pop(0))
    return mixed


class Feasibility:
    """The pool, the trainers and a checker snapshot for one course at one simulated moment."""

    def __init__(self, db: Session, course: Course, now: datetime):
        self.db, self.course, self.now = db, course, now
        self.pool = pool_drivers(db, course.id)
        self.trainers = list(db.scalars(select(Trainer).order_by(Trainer.id)))
        self.snap: Snapshot = load_snapshot_for_ids(
            db, course.id, now, {d.id for d in self.pool}, set(), {t.id for t in self.trainers})
        self.by_id = {d.id: d for d in self.pool}

    # --- Drivers for one session time ----------------------------------------------------------------
    def check_drivers(self, info: SessionInfo, candidates: list[Driver] | None = None) -> tuple[list[Driver], Counter]:
        """(drivers who pass, number of drivers excluded per violation code) for booking into ``info``."""
        passing: list[Driver] = []
        excluded: Counter = Counter()
        for driver in (self.pool if candidates is None else candidates):
            violations = driver_can_book(self.snap, driver, info)
            if violations:
                excluded.update({v.code for v in violations})
            else:
                passing.append(driver)
        return passing, excluded

    def new_session_info(self, start: datetime, end: datetime, capacity: int | None = None) -> SessionInfo:
        return SessionInfo(TEMP_KEY, self.course.id, 0, start, end, capacity or self.course.default_capacity)

    def free_trainers(self, start: datetime, end: datetime) -> list[int]:
        out = []
        for t in self.trainers:
            action = AddSession(type="add_session", temp_ref=TEMP_KEY, course_id=self.course.id, trainer_id=t.id,
                                start=start, end=end, location=DEFAULT_LOCATION, capacity=self.course.default_capacity)
            if not trainer_can_run(self.snap, action):
                out.append(t.id)
        return out

    # --- Windows for a new session ----------------------------------------------------------------------
    def window(self, start: datetime) -> Window:
        end = session_end(start, self.course.duration_hours)
        passing, _ = self.check_drivers(self.new_session_info(start, end))
        return Window(start, end, [d.id for d in passing], dict(Counter(d.shift for d in passing)), self.free_trainers(start, end))

    def windows(self, date_from: date, date_to: date, hours=SESSION_START_HOURS, weekdays: tuple[int, ...] | None = SESSION_WEEKDAYS) -> list[Window]:
        """Candidate windows in a date range on the given start hours, honouring the lead-time policy."""
        earliest = earliest_new_session_start(self.now)
        found = []
        day = date_from
        while day <= date_to:
            if weekdays is None or day.weekday() in weekdays:
                for hour in hours:
                    start = datetime.combine(day, time(hour))
                    if start >= earliest:
                        found.append(self.window(start))
            day += timedelta(days=1)
        return found

    # --- Existing sessions ----------------------------------------------------------------------------------
    def sessions_with_free_seats(self, limit: int | None = None) -> list[tuple[TrainingSession, int]]:
        """Upcoming scheduled sessions of the course that still have free seats, with the free seat count."""
        booked = dict(self.db.execute(
            select(Enrollment.session_id, func.count(Enrollment.id)).where(Enrollment.status == "booked").group_by(Enrollment.session_id)).all())
        rows = self.db.scalars(select(TrainingSession).where(
            TrainingSession.course_id == self.course.id, TrainingSession.status == "scheduled", TrainingSession.start_time > self.now
        ).order_by(TrainingSession.start_time, TrainingSession.id))
        out = [(s, s.capacity - booked.get(s.id, 0)) for s in rows]
        out = [(s, free) for s, free in out if free > 0]
        return out[:limit] if limit else out

    def session_availability(self, session: TrainingSession) -> tuple[list[Driver], Counter]:
        info = SessionInfo(session.id, session.course_id, session.trainer_id, session.start_time, session.end_time, session.capacity)
        return self.check_drivers(info)

    # --- Summaries -----------------------------------------------------------------------------------------------
    def pool_by_shift(self) -> dict[str, int]:
        return dict(sorted(Counter(d.shift for d in self.pool).items()))

    def shift_hours(self, days: int = 7) -> dict[str, dict]:
        """For each shift group: the start hours at which at least one driver of that shift passes on most days."""
        start_day = earliest_new_session_start(self.now).date()
        counts: dict[str, Counter] = {s: Counter() for s in self.pool_by_shift()}
        n_days = 0
        for offset in range(days):
            day = start_day + timedelta(days=offset)
            n_days += 1
            for hour in ALL_START_HOURS:
                start = datetime.combine(day, time(hour))
                end = session_end(start, self.course.duration_hours)
                shifts = {d.shift for d in self.check_drivers(self.new_session_info(start, end))[0]}
                for shift in shifts:
                    counts[shift][hour] += 1
        out = {}
        for shift, per_hour in counts.items():
            hours = sorted(h for h, n in per_hour.items() if n * 2 >= n_days)
            out[shift] = {
                "start_hours_that_work": hours,
                "earliest_start": f"{hours[0]:02d}:00" if hours else None,
                "latest_start": f"{hours[-1]:02d}:00" if hours else None,
            }
        return out


# --- "Does any valid plan exist?" ---------------------------------------------------------------------------------
def assess(feas: Feasibility, days: int = 28) -> dict:
    """Decide whether a valid fix can exist. Returns ``{feasible, code, message, numbers}``.

    Feasible when at least one pool driver can join an existing session with free seats, or a new session on the ERP slot
    grid (Sun-Thu, 09/14/18, after the lead time) has a free trainer and at least one driver who passes.
    """
    pool_size = len(feas.pool)
    numbers: dict = {
        "pool_size": pool_size, "pool_by_shift": feas.pool_by_shift(), "window_days": days,
        "min_lead_hours": int((earliest_new_session_start(feas.now) - feas.now).total_seconds() // 3600),
    }
    if pool_size == 0:
        return {"feasible": False, "code": "pool_empty",
                "message": "Nobody is left who could still take this course this year (everyone eligible has finished it or is already booked).",
                "numbers": numbers}

    existing = feas.sessions_with_free_seats()
    existing_ok = 0
    for session, _free in existing:
        if feas.session_availability(session)[0]:
            existing_ok += 1
    numbers.update({"existing_sessions_with_free_seats": len(existing), "existing_sessions_that_accept_drivers": existing_ok})

    first_day = earliest_new_session_start(feas.now).date()
    windows = feas.windows(first_day, first_day + timedelta(days=days))
    with_drivers = [w for w in windows if w.count > 0]
    usable = [w for w in with_drivers if w.free_trainers]
    numbers.update({"new_session_windows_checked": len(windows), "windows_with_drivers": len(with_drivers), "usable_new_session_windows": len(usable)})
    if existing_ok or usable:
        return {"feasible": True, "code": None, "message": "", "numbers": numbers}

    if with_drivers:
        code, text = "no_trainer_free", "No trainer is free at any time when drivers are available in the next %d days." % days
    elif existing and not existing_ok:
        code, text = "no_driver_available", ("Drivers who still need this course are on shift, on leave or already booked at every session time "
                                             "(existing and new) in the next %d days." % days)
    else:
        code, text = "no_driver_available", "No driver who still needs this course can attend any session time in the next %d days." % days
    return {"feasible": False, "code": code, "message": text, "numbers": numbers}
