"""Tracking: what has actually been completed, week by week, and who is falling behind.

Everything here reads the database once (``load_facts``, a fixed number of queries) and then
works on plain Python objects. The forecasting and risk code reuses the same ``TrackingFacts``
so the dashboard never runs a query per course.

Definitions (used everywhere, keep them in one place):
- ``is_completion``: what counts as a completion.
- ``week_index_dates``: the weeks of the year (Sunday to Saturday).
- ``target_pace``: where we should be by now if the target were spread evenly over the year.
- ``eligible_pool``: how many drivers could still complete a course.
"""
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.models import Course, Driver, Enrollment, TrainingSession, TrainingTarget
from app.services.sim_clock import get_sim_now
from app.simulator.rules import SIM_END, SIM_START, SIM_YEAR, week_start

GROUP_FIELDS = ("shift", "nationality", "depot")
YEAR_START_DT = datetime(SIM_YEAR, 1, 1)
YEAR_END_DT = datetime(SIM_YEAR + 1, 1, 1)


# --- Definitions ------------------------------------------------------------------------
def is_completion(enrollment_status: str, session_status: str, session_start: datetime) -> bool:
    """A completion is an enrollment with status 'attended' in a 'completed' session that started in SIM_YEAR.

    It counts in the week of the session's start time. A driver counts once per course per year
    (Phase 2 refuses a second booking), and the queries below also count distinct drivers where it matters.
    """
    return enrollment_status == "attended" and session_status == "completed" and session_start.year == SIM_YEAR


def week_starts() -> list[date]:
    """Every Sunday from the week containing SIM_START to the week containing SIM_END."""
    first, last = week_start(SIM_START.date()), week_start(SIM_END.date())
    return [first + timedelta(weeks=i) for i in range(((last - first).days // 7) + 1)]


def week_end(ws: date) -> datetime:
    """The moment a week closes: next Sunday 00:00, but never later than SIM_END."""
    return min(datetime.combine(ws + timedelta(days=7), datetime.min.time()), SIM_END)


def year_fraction(t: datetime) -> float:
    """How much of the simulated year has passed at time ``t`` (0.0 to 1.0)."""
    total = (SIM_END - SIM_START).total_seconds()
    return max(0.0, min(1.0, (t - SIM_START).total_seconds() / total))


def target_pace(target: int, t: datetime) -> float:
    """Linear target pace: ``target × (t − SIM_START) / (SIM_END − SIM_START)``.

    Simplification: real demand is not linear (seasons, holidays), so the pace line is a yardstick, not a promise.
    """
    return target * year_fraction(t)


def eligible_drivers(drivers: list["DriverFact"], completed_ids: set[int], now: datetime) -> list["DriverFact"]:
    """Drivers who could still complete a course this year.

    A driver is eligible if they are active now, or are a hire who starts later this year (inactive
    with a hire date after now and on or before SIM_END), and have not completed the course in SIM_YEAR.
    """
    found = []
    for d in drivers:
        pending_hire = (not d.is_active) and now.date() < d.hire_date <= SIM_END.date()
        if (d.is_active or pending_hire) and d.id not in completed_ids:
            found.append(d)
    return found


def eligible_pool(drivers: list["DriverFact"], completed_ids: set[int], now: datetime) -> int:
    """How many drivers could still complete a course (see ``eligible_drivers``).

    No forecast may project more new completions than this number.
    """
    return len(eligible_drivers(drivers, completed_ids, now))


# --- Plain data loaded from the DB ------------------------------------------------------
@dataclass
class CourseFact:
    id: int
    code: str
    name: str
    is_mandatory: bool
    target: int


@dataclass
class SessionFact:
    id: int
    course_id: int
    start_time: datetime
    capacity: int
    status: str
    booked: int  # enrollments with status 'booked' (seats promised, session still to run)
    attended: int
    no_show: int

    @property
    def used(self) -> int:
        """Seats actually used in a finished session: people who turned up plus people who did not."""
        return self.attended + self.no_show


@dataclass
class DriverFact:
    id: int
    shift: str
    nationality: str
    depot: str
    is_active: bool
    hire_date: date


@dataclass
class TrackingFacts:
    now: datetime
    courses: list[CourseFact]
    sessions: list[SessionFact]
    drivers: list[DriverFact]
    # (course_id, driver_id) -> [attended count, no_show count] in completed sessions of SIM_YEAR
    driver_course: dict[tuple[int, int], list[int]] = field(default_factory=dict)

    def sessions_of(self, course_id: int) -> list[SessionFact]:
        return [s for s in self.sessions if s.course_id == course_id]


def load_facts(db: Session) -> TrackingFacts:
    """Read everything tracking needs in five queries, whatever the number of courses or sessions."""
    now = get_sim_now(db)

    courses = [
        CourseFact(c.id, c.code, c.name, c.is_mandatory, target)
        for c, target in db.execute(
            select(Course, TrainingTarget.target_completions)
            .join(TrainingTarget, TrainingTarget.course_id == Course.id)
            .where(TrainingTarget.year == SIM_YEAR)
            .order_by(Course.id)
        )
    ]

    def count_of(status: str):
        return func.coalesce(func.sum(case((Enrollment.status == status, 1), else_=0)), 0)

    sessions = [
        SessionFact(sid, cid, start, cap, status, int(booked), int(att), int(ns))
        for sid, cid, start, cap, status, booked, att, ns in db.execute(
            select(
                TrainingSession.id, TrainingSession.course_id, TrainingSession.start_time,
                TrainingSession.capacity, TrainingSession.status,
                count_of("booked"), count_of("attended"), count_of("no_show"),
            )
            .outerjoin(Enrollment, Enrollment.session_id == TrainingSession.id)
            .where(TrainingSession.start_time >= YEAR_START_DT, TrainingSession.start_time < YEAR_END_DT)
            .group_by(
                TrainingSession.id, TrainingSession.course_id, TrainingSession.start_time,
                TrainingSession.capacity, TrainingSession.status,
            )
            .order_by(TrainingSession.start_time, TrainingSession.id)
        )
    ]

    drivers = [
        DriverFact(d.id, d.shift, d.nationality, d.depot, d.is_active, d.hire_date)
        for d in db.scalars(select(Driver).order_by(Driver.id))
    ]

    driver_course = {
        (cid, did): [int(att), int(ns)]
        for cid, did, att, ns in db.execute(
            select(TrainingSession.course_id, Enrollment.driver_id, count_of("attended"), count_of("no_show"))
            .join(Enrollment, Enrollment.session_id == TrainingSession.id)
            .where(
                TrainingSession.status == "completed",
                TrainingSession.start_time >= YEAR_START_DT,
                TrainingSession.start_time < YEAR_END_DT,
            )
            .group_by(TrainingSession.course_id, Enrollment.driver_id)
        )
    }
    return TrackingFacts(now, courses, sessions, drivers, driver_course)


# --- Weekly series ----------------------------------------------------------------------
def completed_by_week(facts: TrackingFacts, course_id: int) -> dict[date, list[int]]:
    """{week start: [attended, no_show, seats_used, capacity]} for completed sessions of one course (sparse)."""
    weeks: dict[date, list[int]] = defaultdict(lambda: [0, 0, 0, 0])
    for s in facts.sessions:
        if s.course_id != course_id or s.status != "completed":
            continue
        row = weeks[week_start(s.start_time.date())]
        row[0] += s.attended
        row[1] += s.no_show
        row[2] += s.used
        row[3] += s.capacity
    return weeks


def series_from_facts(facts: TrackingFacts, course: CourseFact) -> list[dict]:
    """One row per week that has started by sim now, with empty weeks filled with zero.

    ``as_of`` is when the row's cumulative number is measured: the end of the week, or sim now for the
    week still in progress. ``closed`` is True once the week is over.
    """
    by_week = completed_by_week(facts, course.id)
    rows: list[dict] = []
    cumulative = 0
    for ws in week_starts():
        if datetime.combine(ws, datetime.min.time()) >= facts.now:
            break
        attended, no_show = by_week.get(ws, (0, 0))[:2]
        cumulative += attended
        as_of = min(week_end(ws), facts.now)
        rows.append({
            "course_id": course.id,
            "week_start": ws,
            "as_of": as_of,
            "closed": week_end(ws) <= facts.now,
            "attended_in_week": attended,
            "no_shows_in_week": no_show,
            "cumulative_attended": cumulative,
            "target_pace": round(target_pace(course.target, as_of), 2),
        })
    return rows


def weekly_series(db: Session, course_id: int | None = None) -> list[dict]:
    """Weekly attended, no-shows, cumulative attended and target pace, for one course or all of them.

    Built from the one grouped session query in ``load_facts``; weeks bucket in Python so the same
    code runs on SQLite and Postgres.
    """
    facts = load_facts(db)
    rows: list[dict] = []
    for course in facts.courses:
        if course_id is None or course.id == course_id:
            rows.extend(series_from_facts(facts, course))
    return rows


# --- Per-course counts used by forecasting ---------------------------------------------
def attended_total(facts: TrackingFacts, course_id: int) -> int:
    return sum(s.attended for s in facts.sessions if s.course_id == course_id and s.status == "completed")


def completed_driver_ids(facts: TrackingFacts, course_id: int) -> set[int]:
    return {did for (cid, did), (att, _ns) in facts.driver_course.items() if cid == course_id and att > 0}


# --- Who is falling behind --------------------------------------------------------------
def breakdown_from_facts(facts: TrackingFacts, course_id: int, by: str) -> list[dict]:
    """For each group (shift, nationality or depot): active drivers, drivers who completed, completion %, no-show rate.

    Completion % is completed drivers ÷ active drivers in the group. 'rotating' stays its own shift group.
    """
    if by not in GROUP_FIELDS:
        raise ValueError(f"by must be one of {GROUP_FIELDS}")
    groups: dict[str, dict] = {}
    for d in facts.drivers:
        key = getattr(d, by)
        g = groups.setdefault(key, {"group": key, "active_drivers": 0, "completed": 0, "attended": 0, "no_show": 0})
        if d.is_active:
            g["active_drivers"] += 1
        att, ns = facts.driver_course.get((course_id, d.id), (0, 0))
        if att > 0:
            g["completed"] += 1
        g["attended"] += att
        g["no_show"] += ns
    rows = []
    for g in groups.values():
        trials = g["attended"] + g["no_show"]
        rows.append({
            "group": g["group"],
            "active_drivers": g["active_drivers"],
            "completed": g["completed"],
            "completion_pct": round(100 * g["completed"] / g["active_drivers"], 1) if g["active_drivers"] else 0.0,
            "no_show_rate": round(g["no_show"] / trials, 3) if trials else None,
        })
    return sorted(rows, key=lambda r: (-r["completion_pct"], r["group"]))


def group_breakdown(db: Session, course_id: int, by: str = "shift") -> list[dict]:
    """Same as ``breakdown_from_facts`` but reads the database itself."""
    return breakdown_from_facts(load_facts(db), course_id, by)
