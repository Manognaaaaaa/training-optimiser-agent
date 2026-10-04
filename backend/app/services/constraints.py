"""The hard-constraint checker: is this plan allowed? Plain, deterministic Python. No LLM, no writes.

How it works
1. Read what the plan touches (drivers, sessions, trainers, bookings) into an in-memory ``Snapshot``.
2. Apply the plan's actions to the snapshot one at a time, in order, checking each action first.
   Because the snapshot changes as we go, conflicts *inside* a plan are caught (for example two
   actions that both take the last seat of a session).
3. Collect every violation, never just the first one.

An action that breaks a rule is reported but NOT applied to the snapshot, so one bad step does
not hide or distort the checks on later steps. (A bad ``add_session`` is still registered, so
later steps that use its temp ref are not also reported as unknown.)

The database is only read. Every rule is documented in ``docs/constraint_rules.md``.
"""
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Course, Driver, DriverUnavailability, Enrollment, Trainer, TrainingSession
from app.schemas.plan import Action, AddSession, CheckResult, EnrollDriver, MoveDriver, Plan, Violation
from app.services.sim_clock import get_sim_now
from app.simulator.rules import (
    SHIFT_NIGHT, SIM_YEAR, effective_shift, is_driver_free, session_length_hours, week_start,
)

# --- Tunable numbers ---------------------------------------------------------------------------
MAX_DRIVER_SESSIONS_PER_DAY = 1  # a driver may start at most this many sessions on one calendar day
MIN_REST_HOURS_BETWEEN_SESSIONS = 12  # hours between the end of one session and the start of the driver's next
NIGHT_SHIFT_EARLIEST_START = time(12, 0)  # a driver just off a night shift cannot start a session before this

# Every code and the category it belongs to. Phase 8 uses "hallucination" for the catch-rate metric.
CATEGORY = {
    "UNKNOWN_DRIVER": "hallucination",
    "UNKNOWN_SESSION": "hallucination",
    "UNKNOWN_TRAINER": "hallucination",
    "UNKNOWN_COURSE": "hallucination",
    "UNKNOWN_TEMP_REF": "hallucination",
    "NOT_ENROLLED": "hallucination",
    "INACTIVE_DRIVER": "validity",
    "SESSION_NOT_SCHEDULED": "validity",
    "SESSION_IN_PAST": "validity",
    "WRONG_COURSE": "validity",
    "ALREADY_COMPLETED": "validity",
    "ALREADY_BOOKED": "validity",
    "BAD_SESSION_TIMES": "validity",
    "BAD_CAPACITY": "validity",
    "DUPLICATE_TEMP_REF": "validity",
    "OVER_CAPACITY": "capacity",
    "DRIVER_UNAVAILABLE": "availability",
    "DRIVER_ON_SHIFT": "availability",
    "DRIVER_DOUBLE_BOOKED": "double_booking",
    "TRAINER_DOUBLE_BOOKED": "double_booking",
    "TRAINER_WEEKLY_LOAD": "trainer_load",
    "MAX_SESSIONS_PER_DAY": "rest",
    "MIN_REST_GAP": "rest",
    "NIGHT_SHIFT_MORNING": "rest",
}

SessionKey = int | str  # a real session id, or a temp ref such as "new:1"


def _v(code: str, message: str, entity_type: str, entity_id: int | str | None) -> Violation:
    """Build a violation. The action index is filled in later by ``check_plan``."""
    return Violation(code=code, category=CATEGORY[code], message=message, entity_type=entity_type, entity_id=entity_id)


# --- The in-memory snapshot --------------------------------------------------------------------
@dataclass
class SessionInfo:
    """The few facts about a session that the rules need (works for real and not-yet-created sessions)."""

    key: SessionKey
    course_id: int
    trainer_id: int
    start: datetime
    end: datetime
    capacity: int
    status: str = "scheduled"

    def label(self) -> str:
        kind = "new session" if isinstance(self.key, str) else "session"
        return f"{kind} {self.key} ({self.start:%a %d %b %H:%M})"


@dataclass
class Snapshot:
    now: datetime
    plan_course_id: int
    courses: dict[int, Course] = field(default_factory=dict)
    drivers: dict[int, Driver] = field(default_factory=dict)
    trainers: dict[int, Trainer] = field(default_factory=dict)
    sessions: dict[SessionKey, SessionInfo] = field(default_factory=dict)
    roster: dict[SessionKey, set[int]] = field(default_factory=lambda: defaultdict(set))  # booked drivers per session
    driver_bookings: dict[int, set[SessionKey]] = field(default_factory=lambda: defaultdict(set))
    trainer_sessions: dict[int, set[SessionKey]] = field(default_factory=lambda: defaultdict(set))
    attended_course: set[int] = field(default_factory=set)  # drivers who already attended the plan's course this year
    unavailability: dict[int, list[DriverUnavailability]] = field(default_factory=lambda: defaultdict(list))

    def book(self, driver_id: int, key: SessionKey) -> None:
        self.roster[key].add(driver_id)
        self.driver_bookings[driver_id].add(key)

    def unbook(self, driver_id: int, key: SessionKey) -> None:
        self.roster[key].discard(driver_id)
        self.driver_bookings[driver_id].discard(key)

    def add_session(self, info: SessionInfo) -> None:
        self.sessions[info.key] = info
        if info.trainer_id in self.trainers:
            self.trainer_sessions[info.trainer_id].add(info.key)

    def other_sessions_of_driver(self, driver_id: int, exclude: SessionKey | None) -> list[SessionInfo]:
        """The driver's booked sessions except ``exclude``, earliest first (sorted so results are deterministic)."""
        infos = [self.sessions[k] for k in self.driver_bookings[driver_id] if k != exclude]
        return sorted(infos, key=lambda s: (s.start, str(s.key)))

    def other_sessions_of_trainer(self, trainer_id: int) -> list[SessionInfo]:
        infos = [self.sessions[k] for k in self.trainer_sessions[trainer_id]]
        return sorted(infos, key=lambda s: (s.start, str(s.key)))


def _info_from_row(row: TrainingSession) -> SessionInfo:
    return SessionInfo(row.id, row.course_id, row.trainer_id, row.start_time, row.end_time, row.capacity, row.status)


def _referenced_ids(plan: Plan) -> tuple[set[int], set[int], set[int], set[int]]:
    """The driver, session, trainer and course ids that the plan mentions (temp refs are not ids)."""
    drivers: set[int] = set()
    sessions: set[int] = set()
    trainers: set[int] = set()
    courses = {plan.course_id}
    for action in plan.actions:
        if isinstance(action, MoveDriver):
            drivers.add(action.driver_id)
            sessions.add(action.from_session_id)
            if isinstance(action.to_session_id, int):
                sessions.add(action.to_session_id)
        elif isinstance(action, EnrollDriver):
            drivers.add(action.driver_id)
            if isinstance(action.session_id, int):
                sessions.add(action.session_id)
        else:
            trainers.add(action.trainer_id)
            courses.add(action.course_id)
    return drivers, sessions, trainers, courses


def load_snapshot(db: Session, plan: Plan, now: datetime) -> Snapshot:
    """Read everything the plan touches. Read-only: only SELECT statements are used."""
    driver_ids, session_ids, trainer_ids, course_ids = _referenced_ids(plan)
    snap = Snapshot(now=now, plan_course_id=plan.course_id)

    snap.courses = {c.id: c for c in db.scalars(select(Course).where(Course.id.in_(course_ids)))}
    snap.drivers = {d.id: d for d in db.scalars(select(Driver).where(Driver.id.in_(driver_ids)))}
    snap.trainers = {t.id: t for t in db.scalars(select(Trainer).where(Trainer.id.in_(trainer_ids)))}

    # The sessions the plan names, with who is booked in each.
    for row in db.scalars(select(TrainingSession).where(TrainingSession.id.in_(session_ids))):
        snap.sessions[row.id] = _info_from_row(row)
    booked_in_named = select(Enrollment.session_id, Enrollment.driver_id).where(
        Enrollment.session_id.in_(session_ids), Enrollment.status == "booked"
    )
    for session_id, driver_id in db.execute(booked_in_named):
        snap.roster[session_id].add(driver_id)

    # Every upcoming booking of the drivers involved (for double-booking and rest rules).
    driver_bookings = (
        select(Enrollment.driver_id, TrainingSession)
        .join(TrainingSession, TrainingSession.id == Enrollment.session_id)
        .where(Enrollment.driver_id.in_(driver_ids), Enrollment.status == "booked", TrainingSession.status == "scheduled")
    )
    for driver_id, row in db.execute(driver_bookings):
        snap.sessions[row.id] = _info_from_row(row)
        snap.driver_bookings[driver_id].add(row.id)

    # Every scheduled session of the trainers involved (for double-booking and weekly load).
    for row in db.scalars(
        select(TrainingSession).where(TrainingSession.trainer_id.in_(trainer_ids), TrainingSession.status == "scheduled")
    ):
        snap.sessions[row.id] = _info_from_row(row)
        snap.trainer_sessions[row.trainer_id].add(row.id)

    # Who already attended this course in the target year.
    attended = (
        select(Enrollment.driver_id)
        .join(TrainingSession, TrainingSession.id == Enrollment.session_id)
        .where(
            Enrollment.driver_id.in_(driver_ids),
            Enrollment.status == "attended",
            TrainingSession.course_id == plan.course_id,
            TrainingSession.start_time >= datetime(SIM_YEAR, 1, 1),
            TrainingSession.start_time < datetime(SIM_YEAR + 1, 1, 1),
        )
    )
    snap.attended_course = set(db.scalars(attended))

    for row in db.scalars(select(DriverUnavailability).where(DriverUnavailability.driver_id.in_(driver_ids))):
        snap.unavailability[row.driver_id].append(row)
    return snap


def _overlaps(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    return a_start < b_end and b_start < a_end


# --- Rules for booking a driver into a session --------------------------------------------------
# Each rule takes (snap, driver, session) and returns a list of violations (empty = fine).
def rule_inactive_driver(snap: Snapshot, driver: Driver, session: SessionInfo) -> list[Violation]:
    if driver.is_active:
        return []
    return [_v("INACTIVE_DRIVER", f"{driver.name} is not an active driver.", "driver", driver.id)]


def rule_session_not_scheduled(snap: Snapshot, driver: Driver, session: SessionInfo) -> list[Violation]:
    if session.status == "scheduled":
        return []
    return [_v("SESSION_NOT_SCHEDULED", f"{session.label()} is {session.status}, so nobody can be booked into it.", "session", session.key)]


def rule_session_in_past(snap: Snapshot, driver: Driver, session: SessionInfo) -> list[Violation]:
    if session.start > snap.now:
        return []
    return [_v("SESSION_IN_PAST", f"{session.label()} has already started (it is now {snap.now:%a %d %b %H:%M}).", "session", session.key)]


def rule_wrong_course(snap: Snapshot, driver: Driver, session: SessionInfo) -> list[Violation]:
    if session.course_id == snap.plan_course_id:
        return []
    return [_v("WRONG_COURSE", f"{session.label()} is for a different course than the plan's course ({snap.plan_course_id}).", "session", session.key)]


def rule_already_completed(snap: Snapshot, driver: Driver, session: SessionInfo) -> list[Violation]:
    if driver.id not in snap.attended_course:
        return []
    return [_v("ALREADY_COMPLETED", f"{driver.name} has already attended this course in {SIM_YEAR}.", "driver", driver.id)]


def rule_already_booked(snap: Snapshot, driver: Driver, session: SessionInfo) -> list[Violation]:
    for other in snap.other_sessions_of_driver(driver.id, exclude=None):
        if other.course_id == snap.plan_course_id:
            return [_v("ALREADY_BOOKED", f"{driver.name} is already booked for this course in {other.label()}.", "driver", driver.id)]
    return []


def rule_over_capacity(snap: Snapshot, driver: Driver, session: SessionInfo) -> list[Violation]:
    booked_after = len(snap.roster[session.key]) + 1
    if booked_after <= session.capacity:
        return []
    return [_v("OVER_CAPACITY", f"{session.label()} would have {booked_after} drivers booked but only {session.capacity} seats.", "session", session.key)]


def rule_driver_unavailable(snap: Snapshot, driver: Driver, session: SessionInfo) -> list[Violation]:
    found = []
    for row in snap.unavailability[driver.id]:
        if _overlaps(session.start, session.end, row.start_time, row.end_time):
            found.append(_v("DRIVER_UNAVAILABLE", f"{driver.name} is unavailable ({row.reason}) during {session.label()}.", "driver", driver.id))
    return found


def rule_driver_on_shift(snap: Snapshot, driver: Driver, session: SessionInfo) -> list[Violation]:
    """Reuses the simulator's roster rule: no session during a shift, nor within a few hours after one."""
    ok, reason = is_driver_free(driver, session.start, session.end, [])
    if ok:
        return []
    return [_v("DRIVER_ON_SHIFT", f"{driver.name} cannot attend {session.label()}: {reason}.", "driver", driver.id)]


def rule_driver_double_booked(snap: Snapshot, driver: Driver, session: SessionInfo) -> list[Violation]:
    found = []
    for other in snap.other_sessions_of_driver(driver.id, exclude=session.key):
        if _overlaps(session.start, session.end, other.start, other.end):
            found.append(_v("DRIVER_DOUBLE_BOOKED", f"{driver.name} is already in {other.label()}, which overlaps {session.label()}.", "driver", driver.id))
    return found


def rule_max_sessions_per_day(snap: Snapshot, driver: Driver, session: SessionInfo) -> list[Violation]:
    same_day = [o for o in snap.other_sessions_of_driver(driver.id, exclude=session.key) if o.start.date() == session.start.date()]
    if len(same_day) + 1 <= MAX_DRIVER_SESSIONS_PER_DAY:
        return []
    return [_v(
        "MAX_SESSIONS_PER_DAY",
        f"{driver.name} would have {len(same_day) + 1} sessions on {session.start:%a %d %b}; the limit is {MAX_DRIVER_SESSIONS_PER_DAY}.",
        "driver", driver.id,
    )]


def rule_min_rest_gap(snap: Snapshot, driver: Driver, session: SessionInfo) -> list[Violation]:
    found = []
    for other in snap.other_sessions_of_driver(driver.id, exclude=session.key):
        if _overlaps(session.start, session.end, other.start, other.end):
            continue  # already reported as a double booking
        gap = session.start - other.end if other.end <= session.start else other.start - session.end
        gap_hours = gap.total_seconds() / 3600
        if gap_hours < MIN_REST_HOURS_BETWEEN_SESSIONS:
            found.append(_v(
                "MIN_REST_GAP",
                f"{driver.name} would get only {gap_hours:.1f}h of rest between {other.label()} and {session.label()}; at least {MIN_REST_HOURS_BETWEEN_SESSIONS}h is required.",
                "driver", driver.id,
            ))
    return found


def rule_night_shift_morning(snap: Snapshot, driver: Driver, session: SessionInfo) -> list[Violation]:
    """A driver who worked the night shift that ended this morning cannot start a session early in the day."""
    previous_day = session.start.date() - timedelta(days=1)
    if effective_shift(driver, previous_day) != SHIFT_NIGHT:
        return []
    if session.start.time() >= NIGHT_SHIFT_EARLIEST_START:
        return []
    return [_v(
        "NIGHT_SHIFT_MORNING",
        f"{driver.name} works nights and has just come off shift; {session.label()} starts before {NIGHT_SHIFT_EARLIEST_START:%H:%M}.",
        "driver", driver.id,
    )]


BOOKING_RULES = [
    rule_inactive_driver,
    rule_session_not_scheduled,
    rule_session_in_past,
    rule_wrong_course,
    rule_already_completed,
    rule_already_booked,
    rule_over_capacity,
    rule_driver_unavailable,
    rule_driver_on_shift,
    rule_driver_double_booked,
    rule_max_sessions_per_day,
    rule_min_rest_gap,
    rule_night_shift_morning,
]


def booking_violations(snap: Snapshot, driver: Driver, session: SessionInfo) -> list[Violation]:
    """Run every booking rule for putting ``driver`` into ``session``."""
    found: list[Violation] = []
    for rule in BOOKING_RULES:
        found.extend(rule(snap, driver, session))
    return found


# --- Rules for creating a new session -----------------------------------------------------------
# Each rule takes (snap, action) and returns a list of violations.
def rule_add_unknown_ids(snap: Snapshot, a: AddSession) -> list[Violation]:
    found = []
    if a.course_id not in snap.courses:
        found.append(_v("UNKNOWN_COURSE", f"Course {a.course_id} does not exist.", "course", a.course_id))
    if a.trainer_id not in snap.trainers:
        found.append(_v("UNKNOWN_TRAINER", f"Trainer {a.trainer_id} does not exist.", "trainer", a.trainer_id))
    return found


def rule_add_wrong_course(snap: Snapshot, a: AddSession) -> list[Violation]:
    if a.course_id == snap.plan_course_id:
        return []
    return [_v("WRONG_COURSE", f"New session {a.temp_ref} is for course {a.course_id}, but the plan is for course {snap.plan_course_id}.", "session", a.temp_ref)]


def rule_add_times(snap: Snapshot, a: AddSession) -> list[Violation]:
    problems = []
    if a.end <= a.start:
        problems.append("it ends before (or when) it starts")
    else:
        course = snap.courses.get(a.course_id)
        if course is not None:
            expected = session_length_hours(course.duration_hours)
            actual = (a.end - a.start).total_seconds() / 3600
            if actual != expected:
                problems.append(f"it lasts {actual:g}h but this course's sessions last {expected}h")
    if a.start < datetime(SIM_YEAR, 1, 1) or a.end > datetime(SIM_YEAR + 1, 1, 1):
        problems.append(f"it is not inside {SIM_YEAR}")
    return [_v("BAD_SESSION_TIMES", f"New session {a.temp_ref} has bad times: {problem}.", "session", a.temp_ref) for problem in problems]


def rule_add_in_past(snap: Snapshot, a: AddSession) -> list[Violation]:
    if a.start > snap.now:
        return []
    return [_v("SESSION_IN_PAST", f"New session {a.temp_ref} starts {a.start:%a %d %b %H:%M}, which is not after the current simulated time ({snap.now:%a %d %b %H:%M}).", "session", a.temp_ref)]


def rule_add_capacity(snap: Snapshot, a: AddSession) -> list[Violation]:
    course = snap.courses.get(a.course_id)
    if a.capacity <= 0:
        return [_v("BAD_CAPACITY", f"New session {a.temp_ref} has capacity {a.capacity}; it must be at least 1.", "session", a.temp_ref)]
    if course is not None and a.capacity > course.default_capacity:
        return [_v("BAD_CAPACITY", f"New session {a.temp_ref} has capacity {a.capacity}, above the course maximum of {course.default_capacity}.", "session", a.temp_ref)]
    return []


def rule_add_trainer_double_booked(snap: Snapshot, a: AddSession) -> list[Violation]:
    found = []
    if a.trainer_id in snap.trainers:
        for other in snap.other_sessions_of_trainer(a.trainer_id):
            if _overlaps(a.start, a.end, other.start, other.end):
                found.append(_v("TRAINER_DOUBLE_BOOKED", f"{snap.trainers[a.trainer_id].name} already teaches {other.label()}, which overlaps new session {a.temp_ref}.", "trainer", a.trainer_id))
    return found


def rule_add_trainer_weekly_load(snap: Snapshot, a: AddSession) -> list[Violation]:
    trainer = snap.trainers.get(a.trainer_id)
    if trainer is None:
        return []
    this_week = week_start(a.start.date())
    in_week = [s for s in snap.other_sessions_of_trainer(a.trainer_id) if week_start(s.start.date()) == this_week]
    if len(in_week) + 1 <= trainer.max_sessions_per_week:
        return []
    return [_v(
        "TRAINER_WEEKLY_LOAD",
        f"{trainer.name} would have {len(in_week) + 1} sessions in the week starting {this_week:%d %b}; the limit is {trainer.max_sessions_per_week}.",
        "trainer", trainer.id,
    )]


ADD_SESSION_RULES = [
    rule_add_unknown_ids,
    rule_add_wrong_course,
    rule_add_times,
    rule_add_in_past,
    rule_add_capacity,
    rule_add_trainer_double_booked,
    rule_add_trainer_weekly_load,
]


# --- Applying the actions ------------------------------------------------------------------------
def _find_driver(snap: Snapshot, driver_id: int, found: list[Violation]) -> Driver | None:
    driver = snap.drivers.get(driver_id)
    if driver is None:
        found.append(_v("UNKNOWN_DRIVER", f"Driver {driver_id} does not exist.", "driver", driver_id))
    return driver


def _find_session(snap: Snapshot, ref: SessionKey, found: list[Violation]) -> SessionInfo | None:
    session = snap.sessions.get(ref)
    if session is not None:
        return session
    if isinstance(ref, str):
        found.append(_v("UNKNOWN_TEMP_REF", f"{ref} is used here, but no earlier add_session action created it.", "session", ref))
    else:
        found.append(_v("UNKNOWN_SESSION", f"Session {ref} does not exist.", "session", ref))
    return None


def _check_enroll(snap: Snapshot, a: EnrollDriver) -> list[Violation]:
    found: list[Violation] = []
    driver = _find_driver(snap, a.driver_id, found)
    session = _find_session(snap, a.session_id, found)
    if driver is None or session is None:
        return found
    found.extend(booking_violations(snap, driver, session))
    if not found:
        snap.book(driver.id, session.key)
    return found


def _check_move(snap: Snapshot, a: MoveDriver) -> list[Violation]:
    found: list[Violation] = []
    driver = _find_driver(snap, a.driver_id, found)
    source = _find_session(snap, a.from_session_id, found)
    target = _find_session(snap, a.to_session_id, found)
    if driver is None or source is None or target is None:
        return found

    was_booked = driver.id in snap.roster[source.key]
    if was_booked:
        snap.unbook(driver.id, source.key)  # free the old seat first, so it is not counted against the move
    else:
        found.append(_v("NOT_ENROLLED", f"{driver.name} is not booked into {source.label()}, so cannot be moved out of it.", "driver", driver.id))

    found.extend(booking_violations(snap, driver, target))
    if found:
        if was_booked:
            snap.book(driver.id, source.key)  # the move is rejected, so put the driver back
    else:
        snap.book(driver.id, target.key)
    return found


def _check_add_session(snap: Snapshot, a: AddSession) -> list[Violation]:
    if a.temp_ref in snap.sessions:
        return [_v("DUPLICATE_TEMP_REF", f"{a.temp_ref} is used by more than one add_session action.", "session", a.temp_ref)]
    found: list[Violation] = []
    for rule in ADD_SESSION_RULES:
        found.extend(rule(snap, a))
    snap.add_session(SessionInfo(a.temp_ref, a.course_id, a.trainer_id, a.start, a.end, a.capacity))
    return found


def _check_action(snap: Snapshot, action: Action) -> list[Violation]:
    if isinstance(action, MoveDriver):
        return _check_move(snap, action)
    if isinstance(action, EnrollDriver):
        return _check_enroll(snap, action)
    return _check_add_session(snap, action)


def check_plan(db: Session, plan: Plan, now: datetime | None = None) -> CheckResult:
    """Check a plan against every rule. ``now`` defaults to the simulated clock, never the real one."""
    if now is None:
        now = get_sim_now(db)
    snap = load_snapshot(db, plan, now)

    violations: list[Violation] = []
    if plan.course_id not in snap.courses:
        violations.append(_v("UNKNOWN_COURSE", f"Course {plan.course_id} does not exist.", "course", plan.course_id))

    for index, action in enumerate(plan.actions):
        for violation in _check_action(snap, action):
            violation.action_index = index
            violations.append(violation)
    return CheckResult.from_violations(violations)
