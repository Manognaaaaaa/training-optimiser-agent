"""Tests for the Phase 4 constraint checker.

Most tests use a tiny hand-built world (see ``World``) so each one is easy to read.
Calendar used by the tests (2026):  Sun 1 Mar = "now" in the simulation, Wed 4 Mar = the usual session day.

Why these driver ids? ``app.simulator.rules.rest_days`` gives each driver two weekly rest days from
their id, and a driver cannot attend during a shift. So we pick ids that are off work on the session day:
  ids 1, 2, 7, 8 (day shift)      rest Tue+Wed or Wed+Thu  -> free all day Wed 4 Mar
  id 3           (day shift)      works Wed                -> clashes with a 09:00 session (DRIVER_ON_SHIFT)
  id 9           (night shift)    rest Thu+Fri             -> free all day Fri 6 Mar
  id 15          (rotating shift) rest Thu+Fri             -> free all day Fri 6 Mar and Fri 13 Mar
"""
import random
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import models  # noqa: F401  (registers all tables)
from app.database import Base
from app.models import (
    Course, Driver, DriverUnavailability, Enrollment, SimState, Trainer, TrainingSession,
)
from app.schemas.plan import Plan, Violation, parse_plan
from app.services import constraints
from app.services.constraints import CATEGORY, check_plan

NOW = datetime(2026, 3, 1, 8, 0)  # Sunday
WED = datetime(2026, 3, 4, 9, 0)  # the usual session start: Wednesday 09:00


def at(day: int, hour: int = 9, month: int = 3) -> datetime:
    return datetime(2026, month, day, hour)


# --- The tiny world ----------------------------------------------------------------------------
class World:
    """Course 1 (4h, max 4 seats), course 2 (2h, max 6), trainer 1 (max 3 a week), trainer 2 (max 5)."""

    def __init__(self, db: Session):
        self.db = db
        db.add(SimState(id=1, current_time=NOW))
        db.add_all([
            Course(id=1, code="C1", name="Course One", duration_hours=4, default_capacity=4, is_mandatory=True),
            Course(id=2, code="C2", name="Course Two", duration_hours=2, default_capacity=6, is_mandatory=False),
            Trainer(id=1, name="Trainer One", max_sessions_per_week=3),
            Trainer(id=2, name="Trainer Two", max_sessions_per_week=5),
        ])
        for driver_id in (1, 2, 7, 8, 3):
            self.driver(driver_id)
        self.driver(9, shift="night")
        self.driver(15, shift="rotating")
        db.commit()

    def driver(self, driver_id: int, shift: str = "day", active: bool = True) -> int:
        self.db.add(Driver(
            id=driver_id, employee_code=f"E{driver_id}", name=f"Driver {driver_id}", nationality="X",
            shift=shift, depot="D", hire_date=datetime(2020, 1, 1).date(), is_active=active,
        ))
        self.db.flush()
        return driver_id

    def session(self, course=1, trainer=1, start=WED, hours=None, capacity=4, status="scheduled") -> int:
        hours = hours or {1: 4, 2: 2}[course]
        row = TrainingSession(
            course_id=course, trainer_id=trainer, start_time=start, end_time=start + timedelta(hours=hours),
            location="Room", capacity=capacity, status=status,
        )
        self.db.add(row)
        self.db.flush()
        return row.id

    def book(self, session_id: int, driver_id: int, status: str = "booked") -> None:
        self.db.add(Enrollment(session_id=session_id, driver_id=driver_id, status=status))
        self.db.flush()

    def away(self, driver_id: int, start: datetime, end: datetime) -> None:
        self.db.add(DriverUnavailability(driver_id=driver_id, start_time=start, end_time=end, reason="leave"))
        self.db.flush()


@pytest.fixture()
def w():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False, autoflush=False) as db:
        yield World(db)


# --- Small helpers to write plans ----------------------------------------------------------------
def enroll(driver, session):
    return {"type": "enroll_driver", "driver_id": driver, "session_id": session}


def move(driver, source, target):
    return {"type": "move_driver", "driver_id": driver, "from_session_id": source, "to_session_id": target}


def add(ref="new:1", course=1, trainer=1, start=WED, hours=4, location="Room A", capacity=4):
    return {
        "type": "add_session", "temp_ref": ref, "course_id": course, "trainer_id": trainer,
        "start": start, "end": start + timedelta(hours=hours), "location": location, "capacity": capacity,
    }


def make_plan(*actions, course=1):
    return {"course_id": course, "actions": list(actions)}


def run(w, raw, now=NOW):
    plan = parse_plan(raw)
    assert isinstance(plan, Plan), plan
    return check_plan(w.db, plan, now)


def codes(result):
    return [v.code for v in result.violations]


def fires(w, raw, code):
    assert code in codes(run(w, raw)), codes(run(w, raw))


def clean_of(w, raw, code):
    assert code not in codes(run(w, raw)), codes(run(w, raw))


# --- A valid plan -------------------------------------------------------------------------------
def test_simple_valid_enroll(w):
    s = w.session()
    result = run(w, make_plan(enroll(1, s)))
    assert result.is_valid and result.violations == [] and result.summary == {}


def test_fully_valid_multi_action_plan(w):
    old = w.session(start=at(5, 9))  # Thu
    w.book(old, 2)
    existing = w.session(start=WED)
    plan = make_plan(
        add("new:1", trainer=2, start=at(4, 14)),  # a new Wed 14:00 session
        enroll(1, existing),
        enroll(7, "new:1"),
        move(2, old, existing),
    )
    result = run(w, plan)
    assert result.violations == []
    assert result.is_valid


# --- Hallucination ------------------------------------------------------------------------------
def test_unknown_driver_fires_and_ok(w):
    s = w.session()
    fires(w, make_plan(enroll(999, s)), "UNKNOWN_DRIVER")
    clean_of(w, make_plan(enroll(1, s)), "UNKNOWN_DRIVER")


def test_unknown_session_fires_and_ok(w):
    s = w.session()
    fires(w, make_plan(enroll(1, 999)), "UNKNOWN_SESSION")
    fires(w, make_plan(move(1, 999, s)), "UNKNOWN_SESSION")
    clean_of(w, make_plan(enroll(1, s)), "UNKNOWN_SESSION")


def test_unknown_trainer_fires_and_ok(w):
    fires(w, make_plan(add(trainer=99)), "UNKNOWN_TRAINER")
    clean_of(w, make_plan(add(trainer=2)), "UNKNOWN_TRAINER")


def test_unknown_course_fires_and_ok(w):
    s = w.session()
    plan_level = run(w, make_plan(enroll(1, s), course=99))
    unknown = [v for v in plan_level.violations if v.code == "UNKNOWN_COURSE"]
    assert len(unknown) == 1 and unknown[0].action_index is None
    fires(w, make_plan(add(course=99)), "UNKNOWN_COURSE")
    clean_of(w, make_plan(enroll(1, s)), "UNKNOWN_COURSE")


def test_unknown_temp_ref_fires_and_ok(w):
    fires(w, make_plan(add("new:1"), enroll(1, "new:9")), "UNKNOWN_TEMP_REF")
    fires(w, make_plan(enroll(1, "new:1")), "UNKNOWN_TEMP_REF")  # used before it is created
    clean_of(w, make_plan(add("new:1"), enroll(1, "new:1")), "UNKNOWN_TEMP_REF")


def test_add_session_then_enroll_new_ref_works(w):
    result = run(w, make_plan(add("new:1"), enroll(1, "new:1")))
    assert result.is_valid


def test_unknown_temp_ref_is_reported_on_the_right_action(w):
    result = run(w, make_plan(add("new:1"), enroll(1, "new:9")))
    bad = [v for v in result.violations if v.code == "UNKNOWN_TEMP_REF"]
    assert [(v.action_index, v.entity_id) for v in bad] == [(1, "new:9")]


def test_not_enrolled_fires_and_ok(w):
    a, b = w.session(start=at(4)), w.session(start=at(5))
    w.book(a, 2)
    fires(w, make_plan(move(1, a, b)), "NOT_ENROLLED")  # driver 1 is not in a
    clean_of(w, make_plan(move(2, a, b)), "NOT_ENROLLED")


# --- Validity -----------------------------------------------------------------------------------
def test_inactive_driver_fires_and_ok(w):
    s = w.session()
    inactive = w.driver(20, active=False)  # id 20 rests Wed+Thu
    fires(w, make_plan(enroll(inactive, s)), "INACTIVE_DRIVER")
    clean_of(w, make_plan(enroll(1, s)), "INACTIVE_DRIVER")


def test_session_not_scheduled_fires_and_ok(w):
    cancelled = w.session(status="cancelled")
    completed = w.session(status="completed")
    fine = w.session()
    fires(w, make_plan(enroll(1, cancelled)), "SESSION_NOT_SCHEDULED")
    fires(w, make_plan(enroll(1, completed)), "SESSION_NOT_SCHEDULED")
    clean_of(w, make_plan(enroll(1, fine)), "SESSION_NOT_SCHEDULED")


def test_session_in_past_fires_and_ok(w):
    past = w.session(start=at(25, 9, month=2))
    future = w.session()
    fires(w, make_plan(enroll(1, past)), "SESSION_IN_PAST")
    clean_of(w, make_plan(enroll(1, future)), "SESSION_IN_PAST")
    fires(w, make_plan(add(start=at(25, 9, month=2))), "SESSION_IN_PAST")
    clean_of(w, make_plan(add()), "SESSION_IN_PAST")


def test_wrong_course_fires_and_ok(w):
    other_course = w.session(course=2)
    same_course = w.session()
    fires(w, make_plan(enroll(1, other_course)), "WRONG_COURSE")
    fires(w, make_plan(add(course=2, hours=2, capacity=4)), "WRONG_COURSE")
    clean_of(w, make_plan(enroll(1, same_course)), "WRONG_COURSE")
    clean_of(w, make_plan(add()), "WRONG_COURSE")


def test_already_completed_fires_and_ok(w):
    done = w.session(start=at(10, 9, month=2), status="completed")
    w.book(done, 1, status="attended")
    target = w.session()
    fires(w, make_plan(enroll(1, target)), "ALREADY_COMPLETED")
    clean_of(w, make_plan(enroll(2, target)), "ALREADY_COMPLETED")


def test_already_completed_ignores_other_courses_and_other_years(w):
    other_course = w.session(course=2, start=at(10, 9, month=2), status="completed")
    w.book(other_course, 1, status="attended")
    last_year = w.session(start=datetime(2025, 6, 4, 9), status="completed")
    w.book(last_year, 2, status="attended")
    target = w.session()
    clean_of(w, make_plan(enroll(1, target)), "ALREADY_COMPLETED")
    clean_of(w, make_plan(enroll(2, target)), "ALREADY_COMPLETED")


def test_already_booked_fires_and_ok(w):
    elsewhere = w.session(start=at(5, 9))
    w.book(elsewhere, 1)
    target = w.session()
    fires(w, make_plan(enroll(1, target)), "ALREADY_BOOKED")
    clean_of(w, make_plan(enroll(2, target)), "ALREADY_BOOKED")


def test_already_booked_other_course_does_not_count(w):
    other_course = w.session(course=2, start=at(5, 9))
    w.book(other_course, 1)
    clean_of(w, make_plan(enroll(1, w.session())), "ALREADY_BOOKED")


def test_move_is_not_blocked_by_the_booking_it_leaves(w):
    a, b = w.session(start=at(4)), w.session(start=at(5))
    w.book(a, 2)  # driver 2 rests Wed+Thu
    result = run(w, make_plan(move(2, a, b)))
    assert result.is_valid, codes(result)


def test_bad_session_times_fires_and_ok(w):
    fires(w, make_plan(add(hours=-1)), "BAD_SESSION_TIMES")  # ends before it starts
    fires(w, make_plan(add(hours=3)), "BAD_SESSION_TIMES")  # course 1 sessions are 4h
    fires(w, make_plan(add(start=datetime(2027, 1, 6, 9))), "BAD_SESSION_TIMES")  # not in 2026
    clean_of(w, make_plan(add()), "BAD_SESSION_TIMES")


def test_long_courses_use_the_four_hour_session_cap(w):
    """A course of 8 hours is taught in 4h sessions, exactly like the seed data does."""
    w.db.add(Course(id=3, code="C3", name="Long", duration_hours=8, default_capacity=4, is_mandatory=False))
    w.db.commit()
    assert "BAD_SESSION_TIMES" not in codes(run(w, make_plan(add(course=3, hours=4), course=3)))
    assert "BAD_SESSION_TIMES" in codes(run(w, make_plan(add(course=3, hours=8), course=3)))


def test_bad_capacity_fires_and_ok(w):
    fires(w, make_plan(add(capacity=0)), "BAD_CAPACITY")
    fires(w, make_plan(add(capacity=-3)), "BAD_CAPACITY")
    fires(w, make_plan(add(capacity=5)), "BAD_CAPACITY")  # course 1 maximum is 4
    clean_of(w, make_plan(add(capacity=4)), "BAD_CAPACITY")


def test_duplicate_temp_ref_fires_and_ok(w):
    fires(w, make_plan(add("new:1"), add("new:1", trainer=2)), "DUPLICATE_TEMP_REF")
    clean_of(w, make_plan(add("new:1"), add("new:2", trainer=2)), "DUPLICATE_TEMP_REF")


# --- Capacity -----------------------------------------------------------------------------------
def test_over_capacity_fires_and_ok(w):
    s = w.session(capacity=2)
    w.book(s, 7)
    fires(w, make_plan(enroll(1, s), enroll(2, s)), "OVER_CAPACITY")
    clean_of(w, make_plan(enroll(1, s)), "OVER_CAPACITY")


def test_two_actions_fill_the_last_seat_second_one_fails(w):
    s = w.session(capacity=1)
    result = run(w, make_plan(enroll(1, s), enroll(2, s)))
    over = [(v.code, v.action_index) for v in result.violations if v.code == "OVER_CAPACITY"]
    assert over == [("OVER_CAPACITY", 1)]


def test_move_into_a_full_session_fires(w):
    full = w.session(start=at(5), capacity=1)
    w.book(full, 7)
    source = w.session(start=at(4))
    w.book(source, 2)
    fires(w, make_plan(move(2, source, full)), "OVER_CAPACITY")


def test_move_frees_the_old_seat_for_later_actions(w):
    a = w.session(start=at(4), capacity=1)
    b = w.session(start=at(5))
    w.book(a, 2)
    result = run(w, make_plan(move(2, a, b), enroll(1, a)))
    assert result.is_valid, codes(result)


# --- Driver availability ------------------------------------------------------------------------
def test_driver_unavailable_fires_and_ok(w):
    s = w.session()
    w.away(1, at(4, 0), at(5, 0))
    fires(w, make_plan(enroll(1, s)), "DRIVER_UNAVAILABLE")
    clean_of(w, make_plan(enroll(2, s)), "DRIVER_UNAVAILABLE")


def test_unavailability_on_another_day_is_fine(w):
    s = w.session()
    w.away(1, at(5, 0), at(6, 0))
    clean_of(w, make_plan(enroll(1, s)), "DRIVER_UNAVAILABLE")


def test_driver_on_shift_fires_and_ok(w):
    s = w.session()
    fires(w, make_plan(enroll(3, s)), "DRIVER_ON_SHIFT")  # driver 3 works Wednesdays 06:00-14:00
    clean_of(w, make_plan(enroll(1, s)), "DRIVER_ON_SHIFT")


# --- Double booking -----------------------------------------------------------------------------
def test_driver_double_booked_fires_and_ok(w):
    other = w.session(course=2, start=at(4, 11))  # 11:00-13:00
    w.book(other, 1)
    target = w.session()  # 09:00-13:00
    fires(w, make_plan(enroll(1, target)), "DRIVER_DOUBLE_BOOKED")
    clean_of(w, make_plan(enroll(2, target)), "DRIVER_DOUBLE_BOOKED")


def test_back_to_back_sessions_do_not_overlap(w):
    other = w.session(course=2, start=at(4, 13))  # 13:00-15:00 starts exactly when the target ends
    w.book(other, 1)
    clean_of(w, make_plan(enroll(1, w.session())), "DRIVER_DOUBLE_BOOKED")


def test_driver_double_booked_inside_one_plan(w):
    first = w.session(start=at(4, 9))
    second = w.session(start=at(4, 11), trainer=2)
    result = run(w, make_plan(enroll(1, first), enroll(1, second)))
    bad = [v for v in result.violations if v.code == "DRIVER_DOUBLE_BOOKED"]
    assert [v.action_index for v in bad] == [1]


def test_driver_double_booked_with_session_added_earlier_in_plan(w):
    existing = w.session(trainer=2, start=at(4, 11))
    result = run(w, make_plan(add("new:1", start=WED), enroll(1, existing), enroll(1, "new:1")))
    bad = [v for v in result.violations if v.code == "DRIVER_DOUBLE_BOOKED"]
    assert [v.action_index for v in bad] == [2]


def test_trainer_double_booked_fires_and_ok(w):
    w.session(trainer=1, start=WED)  # trainer 1 teaches Wed 09:00-13:00
    fires(w, make_plan(add(trainer=1, start=at(4, 11))), "TRAINER_DOUBLE_BOOKED")
    clean_of(w, make_plan(add(trainer=2, start=at(4, 11))), "TRAINER_DOUBLE_BOOKED")


def test_trainer_double_booked_by_session_added_in_same_plan(w):
    result = run(w, make_plan(add("new:1", trainer=1), add("new:2", trainer=1, start=at(4, 11))))
    bad = [v for v in result.violations if v.code == "TRAINER_DOUBLE_BOOKED"]
    assert [v.action_index for v in bad] == [1]


def test_cancelled_sessions_do_not_block_a_trainer(w):
    w.session(trainer=1, start=WED, status="cancelled")
    clean_of(w, make_plan(add(trainer=1)), "TRAINER_DOUBLE_BOOKED")


# --- Trainer load -------------------------------------------------------------------------------
def test_trainer_weekly_load_fires_and_ok(w):
    for day in (1, 2, 3):  # Sun, Mon, Tue: trainer 1 is at its limit of 3
        w.session(trainer=1, start=at(day, 14))
    fires(w, make_plan(add(trainer=1, start=at(4, 14))), "TRAINER_WEEKLY_LOAD")
    clean_of(w, make_plan(add(trainer=2, start=at(4, 14))), "TRAINER_WEEKLY_LOAD")


def test_trainer_weekly_load_counts_sunday_to_saturday_weeks(w):
    for day in (8, 9, 10):  # the NEXT week (Sun 8 Mar onwards)
        w.session(trainer=1, start=at(day, 14))
    clean_of(w, make_plan(add(trainer=1, start=at(4, 14))), "TRAINER_WEEKLY_LOAD")  # Wed 4 Mar is the week before
    fires(w, make_plan(add(trainer=1, start=at(11, 14))), "TRAINER_WEEKLY_LOAD")


def test_trainer_weekly_load_counts_sessions_added_in_same_plan(w):
    plan = make_plan(
        add("new:1", start=at(1, 14)), add("new:2", start=at(2, 14)),
        add("new:3", start=at(3, 14)), add("new:4", start=at(4, 14)),
    )
    bad = [v for v in run(w, plan).violations if v.code == "TRAINER_WEEKLY_LOAD"]
    assert [v.action_index for v in bad] == [3]


# --- Rest rules ---------------------------------------------------------------------------------
def test_max_sessions_per_day_fires_and_ok(w):
    morning = w.session(start=at(4, 9))
    w.book(morning, 1)
    evening = w.session(start=at(4, 18), trainer=2)
    fires(w, make_plan(enroll(1, evening)), "MAX_SESSIONS_PER_DAY")
    clean_of(w, make_plan(enroll(2, evening)), "MAX_SESSIONS_PER_DAY")


def test_max_sessions_per_day_is_tunable(w, monkeypatch):
    morning = w.session(start=at(4, 9))
    w.book(morning, 1)
    evening = w.session(start=at(4, 18), trainer=2)
    monkeypatch.setattr(constraints, "MAX_DRIVER_SESSIONS_PER_DAY", 2)
    monkeypatch.setattr(constraints, "MIN_REST_HOURS_BETWEEN_SESSIONS", 0)
    result = codes(run(w, make_plan(enroll(1, evening))))
    assert "MAX_SESSIONS_PER_DAY" not in result and "MIN_REST_GAP" not in result


def test_min_rest_gap_fires_and_ok(w):
    morning = w.session(start=at(4, 9))  # ends 13:00
    w.book(morning, 2)
    evening = w.session(start=at(4, 18), trainer=2)  # 5h later
    next_day = w.session(start=at(5, 9), trainer=2)  # 20h later (driver 2 also rests Thursday)
    fires(w, make_plan(enroll(2, evening)), "MIN_REST_GAP")
    clean_of(w, make_plan(enroll(2, next_day)), "MIN_REST_GAP")


def test_min_rest_gap_is_tunable(w, monkeypatch):
    morning = w.session(start=at(4, 9))
    w.book(morning, 2)
    next_day = w.session(start=at(5, 9), trainer=2)  # 20h gap
    monkeypatch.setattr(constraints, "MIN_REST_HOURS_BETWEEN_SESSIONS", 24)
    fires(w, make_plan(enroll(2, next_day)), "MIN_REST_GAP")


def test_night_shift_morning_fires_and_ok(w):
    morning = w.session(start=at(6, 9), trainer=2)  # Fri 09:00
    afternoon = w.session(start=at(6, 14), trainer=2)
    evening = w.session(start=at(6, 18), trainer=2)
    fires(w, make_plan(enroll(9, morning)), "NIGHT_SHIFT_MORNING")
    clean_of(w, make_plan(enroll(9, afternoon)), "NIGHT_SHIFT_MORNING")
    clean_of(w, make_plan(enroll(9, evening)), "NIGHT_SHIFT_MORNING")


def test_night_shift_morning_ignores_day_drivers(w):
    clean_of(w, make_plan(enroll(1, w.session(start=WED))), "NIGHT_SHIFT_MORNING")


def test_night_shift_morning_follows_the_rotation(w):
    """Driver 15 rotates: day shift in even ISO weeks, night in odd ones (Thu 5 Mar is week 10, Thu 12 Mar week 11)."""
    after_day_week = w.session(start=at(6, 9), trainer=2)
    after_night_week = w.session(start=at(13, 9), trainer=2)
    clean_of(w, make_plan(enroll(15, after_day_week)), "NIGHT_SHIFT_MORNING")
    fires(w, make_plan(enroll(15, after_night_week)), "NIGHT_SHIFT_MORNING")


def test_night_shift_earliest_start_is_tunable(w, monkeypatch):
    morning = w.session(start=at(6, 9), trainer=2)
    monkeypatch.setattr(constraints, "NIGHT_SHIFT_EARLIEST_START", datetime(2000, 1, 1, 8, 0).time())
    clean_of(w, make_plan(enroll(9, morning)), "NIGHT_SHIFT_MORNING")


# --- Several rules at once, in order ----------------------------------------------------------------
def test_plan_breaking_many_rules_reports_all_with_right_indexes(w):
    cancelled = w.session(status="cancelled")
    other_course = w.session(course=2, start=at(4, 14))
    target = w.session()
    plan = make_plan(
        enroll(999, target),            # 0: unknown driver
        enroll(1, cancelled),           # 1: session not scheduled
        enroll(3, target),              # 2: driver 3 is on shift
        enroll(1, other_course),        # 3: wrong course
        add(trainer=2, capacity=9, hours=3),  # 4: bad capacity and bad times
        enroll(2, "new:7"),             # 5: unknown temp ref
    )
    found = {(v.code, v.action_index) for v in run(w, plan).violations}
    assert found == {
        ("UNKNOWN_DRIVER", 0),
        ("SESSION_NOT_SCHEDULED", 1),
        ("DRIVER_ON_SHIFT", 2),
        ("WRONG_COURSE", 3),
        ("BAD_CAPACITY", 4),
        ("BAD_SESSION_TIMES", 4),
        ("UNKNOWN_TEMP_REF", 5),
    }


def test_summary_counts_by_category(w):
    s = w.session(capacity=1)
    result = run(w, make_plan(enroll(999, s), enroll(1, s), enroll(2, s)))
    assert result.is_valid is False
    assert result.summary == {"hallucination": 1, "capacity": 1}


def test_every_code_has_a_known_category():
    assert set(CATEGORY.values()) == {
        "hallucination", "validity", "capacity", "availability", "double_booking", "trainer_load", "rest",
    }


# --- Schema -------------------------------------------------------------------------------------
def _valid_action():
    return {"type": "enroll_driver", "driver_id": 1, "session_id": 2}


BAD_PLANS = [
    pytest.param(None, id="none"),
    pytest.param("not a plan", id="string"),
    pytest.param([1, 2, 3], id="list"),
    pytest.param({}, id="empty-dict"),
    pytest.param({"course_id": 1}, id="no-actions"),
    pytest.param({"course_id": 1, "actions": []}, id="empty-actions"),
    pytest.param({"course_id": 1, "actions": [_valid_action()] * 51}, id="too-many-actions"),
    pytest.param({"course_id": 1, "actions": [{"type": "teleport_driver"}]}, id="unknown-action-type"),
    pytest.param({"course_id": 1, "actions": [{"driver_id": 1, "session_id": 2}]}, id="missing-type"),
    pytest.param({"course_id": 1, "actions": [{**_valid_action(), "extra": 1}]}, id="extra-key"),
    pytest.param({"course_id": 1, "actions": [{**_valid_action(), "driver_id": "7"}]}, id="id-as-string"),
    pytest.param({"course_id": 1, "actions": [{**_valid_action(), "driver_id": True}]}, id="id-as-bool"),
    pytest.param({"course_id": 1, "actions": [{**_valid_action(), "session_id": "new:abc"}]}, id="bad-temp-ref"),
    pytest.param({"course_id": 1, "actions": [{**_valid_action(), "session_id": None}]}, id="null-session"),
    pytest.param({"course_id": "one", "actions": [_valid_action()]}, id="course-not-a-number"),
    pytest.param({"course_id": 1, "actions": [{**add(), "start": "yesterday-ish"}]}, id="unparseable-time"),
    pytest.param({"course_id": 1, "actions": [{**add(), "start": "2026-03-04T09:00:00+04:00"}]}, id="timezone-aware"),
    pytest.param({"course_id": 1, "actions": [{**add(), "location": ""}]}, id="empty-location"),
    pytest.param({"course_id": 1, "actions": [_valid_action()], "rationale": 5}, id="rationale-not-text"),
]


@pytest.mark.parametrize("raw", BAD_PLANS)
def test_malformed_plans_become_schema_invalid(raw):
    result = parse_plan(raw)
    assert isinstance(result, Violation)
    assert result.code == "SCHEMA_INVALID" and result.category == "schema" and result.message


def test_good_plans_parse_including_iso_strings():
    raw = make_plan(
        {**add(), "start": "2026-03-04T09:00:00", "end": "2026-03-04T13:00:00"},
        enroll(1, "new:1"), move(2, 5, "new:1"),
    )
    raw["alert_id"], raw["rationale"] = 3, "why"
    assert isinstance(parse_plan(raw), Plan)


# --- Properties of the checker ----------------------------------------------------------------------
def test_same_plan_gives_identical_results(w):
    s = w.session(capacity=1)
    plan = parse_plan(make_plan(enroll(999, s), enroll(1, s), enroll(2, s), add(capacity=0)))
    first, second = check_plan(w.db, plan, NOW), check_plan(w.db, plan, NOW)
    assert first == second
    assert first.model_dump() == second.model_dump()


def test_checker_writes_nothing(w):
    a = w.session(start=at(4))
    w.book(a, 2)
    w.db.commit()
    plan = parse_plan(make_plan(add("new:1"), enroll(1, "new:1"), move(2, a, "new:1"), enroll(999, a)))

    def counts():
        return {t.name: w.db.scalar(select(func.count()).select_from(t)) for t in Base.metadata.sorted_tables}

    before = counts()
    check_plan(w.db, plan, NOW)
    assert counts() == before
    assert not w.db.new and not w.db.dirty and not w.db.deleted


def test_sessions_are_past_or_future_by_sim_time_not_real_time(w):
    s = w.session(start=WED)
    raw = make_plan(enroll(1, s))
    plan = parse_plan(raw)
    assert check_plan(w.db, plan).is_valid  # no `now` given: uses sim_state (Sun 1 Mar)

    w.db.get(SimState, 1).current_time = datetime(2026, 3, 10, 8, 0)
    w.db.commit()
    result = check_plan(w.db, plan)
    assert "SESSION_IN_PAST" in codes(result)  # the same session, now in the simulated past


# --- Hallucination catch rate -------------------------------------------------------------------------
FAKE_ID_BASE = 9_000_000


def _real_action(rng, ids, ref_number):
    kind = rng.choice(["move_driver", "enroll_driver", "add_session"])
    if kind == "move_driver":
        return move(rng.choice(ids["drivers"]), rng.choice(ids["sessions"]), rng.choice(ids["sessions"]))
    if kind == "enroll_driver":
        return enroll(rng.choice(ids["drivers"]), rng.choice(ids["sessions"]))
    return add(
        f"new:{ref_number}", course=rng.choice(ids["courses"]), trainer=rng.choice(ids["trainers"]),
        start=datetime(2026, 6, 7, 9), hours=4, capacity=5,
    )


def _inject_fake(rng, action, ids, fake_id):
    """Corrupt one field of ``action`` with something that does not exist. Returns the code that must catch it."""
    kind = action["type"]
    if kind == "enroll_driver":
        choice = rng.choice(["driver", "session", "temp_ref"])
        if choice == "driver":
            action["driver_id"] = fake_id
            return "UNKNOWN_DRIVER"
        if choice == "session":
            action["session_id"] = fake_id
            return "UNKNOWN_SESSION"
        action["session_id"] = "new:99"
        return "UNKNOWN_TEMP_REF"
    if kind == "move_driver":
        choice = rng.choice(["driver", "from", "to", "temp_ref", "not_enrolled"])
        if choice == "driver":
            action["driver_id"] = fake_id
            return "UNKNOWN_DRIVER"
        if choice == "from":
            action["from_session_id"] = fake_id
            return "UNKNOWN_SESSION"
        if choice == "to":
            action["to_session_id"] = fake_id
            return "UNKNOWN_SESSION"
        if choice == "temp_ref":
            action["to_session_id"] = "new:99"
            return "UNKNOWN_TEMP_REF"
        action["driver_id"], action["from_session_id"] = rng.choice(ids["not_booked"])
        return "NOT_ENROLLED"
    if rng.random() < 0.5:
        action["trainer_id"] = fake_id
        return "UNKNOWN_TRAINER"
    action["course_id"] = fake_id
    return "UNKNOWN_COURSE"


def test_hallucination_catch_rate_is_100_percent(db):
    rng = random.Random(2026)
    drivers = list(db.scalars(select(Driver.id)))
    sessions = list(db.scalars(select(TrainingSession.id)))
    booked = {tuple(row) for row in db.execute(select(Enrollment.session_id, Enrollment.driver_id).where(Enrollment.status == "booked"))}
    not_booked = []
    while len(not_booked) < 50:
        pair = (rng.choice(drivers), rng.choice(sessions))
        if (pair[1], pair[0]) not in booked:
            not_booked.append(pair)
    ids = {
        "drivers": drivers, "sessions": sessions, "not_booked": not_booked,
        "trainers": list(db.scalars(select(Trainer.id))), "courses": list(db.scalars(select(Course.id))),
    }

    injected = caught = 0
    for n in range(200):
        actions = [_real_action(rng, ids, ref_number=i + 1) for i in range(rng.randint(2, 6))]
        expected = []  # (code, action_index) pairs that MUST be reported
        for index in rng.sample(range(len(actions)), k=rng.randint(1, min(2, len(actions)))):
            code = _inject_fake(rng, actions[index], ids, FAKE_ID_BASE + n)
            expected.append((code, index))
        plan = parse_plan({"course_id": rng.choice(ids["courses"]), "actions": actions})
        assert isinstance(plan, Plan)
        found = {(v.code, v.action_index) for v in check_plan(db, plan).violations}
        for pair in expected:
            injected += 1
            caught += pair in found

    assert injected >= 200
    assert caught == injected, f"caught {caught} of {injected} injected fake references"


# --- API ------------------------------------------------------------------------------------------
def test_api_check_reports_violations(client):
    body = {"course_id": 1, "actions": [{"type": "enroll_driver", "driver_id": 9_999_999, "session_id": 1}]}
    response = client.post("/api/constraints/check", json=body)
    assert response.status_code == 200
    data = response.json()
    assert data["is_valid"] is False
    assert [(v["code"], v["category"], v["action_index"]) for v in data["violations"]] == [("UNKNOWN_DRIVER", "hallucination", 0)]
    assert data["summary"] == {"hallucination": 1}


def test_api_check_malformed_plan_is_schema_invalid_not_an_error(client):
    for body in ({"nonsense": True}, [1, 2], "text", 42):
        response = client.post("/api/constraints/check", json=body)
        assert response.status_code == 200
        assert response.json()["violations"][0]["code"] == "SCHEMA_INVALID"


def test_api_check_does_not_write(client, db):
    before = {t.name: db.scalar(select(func.count()).select_from(t)) for t in Base.metadata.sorted_tables}
    client.post("/api/constraints/check", json={"course_id": 1, "actions": [{"type": "enroll_driver", "driver_id": 1, "session_id": 1}]})
    after = {t.name: db.scalar(select(func.count()).select_from(t)) for t in Base.metadata.sorted_tables}
    assert before == after
