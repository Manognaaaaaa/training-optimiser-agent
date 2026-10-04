"""Synthetic data generator: one realistic training year, fully determined by the seed.

Run from backend/:  python -m app.simulator.seed --seed 42 --reset
"""
import argparse
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app import models  # noqa: F401  (registers all tables)
from app.database import Base
from app.models import (
    Course, Driver, DriverUnavailability, SimState, Trainer, TrainingSession, TrainingTarget,
)
from app.services.audit import log_event
from app.simulator import scenarios
from app.simulator.engine import book_window
from app.simulator.names import DEPOTS, NATIONALITIES, TRAINER_NAMES
from app.simulator.rng import rng_for
from app.simulator.views import create_views, drop_views
from app.simulator.rules import (
    SESSION_START_HOURS, SESSION_WEEKDAYS, SIM_START, SIM_YEAR, session_end, week_start,
)

DRIVER_COUNT = 300
SHIFT_WEIGHTS = {"day": 50, "night": 35, "rotating": 15}
NEW_HIRE_SHARE = 0.05  # drivers hired during 2026 (inactive until their hire date)


@dataclass(frozen=True)
class CourseProfile:
    code: str
    name: str
    duration_hours: int
    capacity: int
    mandatory: bool
    target_share: tuple[float, float]  # target as a share of active drivers (random within the range)
    capacity_factor: float  # planned capacity / target: how good the plan looks on paper
    preferred_trainer_id: int | None = None  # gets ~85% of this course's sessions


COURSES = [
    CourseProfile("DEF", "Defensive Driving", 8, 12, True, (0.88, 0.92), 1.15),
    # RSR is the healthy control: a lower target (74-78% of drivers) and 145% planned seats, so it finishes 14-29%
    # above target. The old 85-87% target with 124% seats finished 0-14% above (once exactly on target), which is not
    # healthy; more seats instead of a lower target runs out of unfinished drivers and breaks the forecast's pool cap
    # (see docs/forecast_backtest.md).
    CourseProfile("RSR", "Road Safety Refresher", 4, 15, True, (0.74, 0.78), 1.45),
    CourseProfile("FA", "First Aid", 6, 10, True, (0.88, 0.92), 1.20, scenarios.LEAVE_TRAINER_ID),
    CourseProfile("HEAT", "Heat Stress Awareness", 2, 15, True, (0.85, 0.92), 1.15),
    CourseProfile("FUEL", "Fuel-Efficient Driving", 3, 12, False, (0.25, 0.35), 1.10),
    CourseProfile("CS", "Customer Service", 3, 15, False, (0.20, 0.30), 1.10),
    CourseProfile("EV", "EV Handling", 4, 8, False, (0.08, 0.12), 1.10),
    CourseProfile("HAZ", "Hazmat Awareness", 5, 8, False, (0.10, 0.15), 1.15, scenarios.LEAVE_TRAINER_ID),
]

UNAVAILABILITY_REASONS = ["medical appointment", "visa renewal", "court hearing", "family emergency", "licence renewal"]


# --- Drivers, trainers, courses ---------------------------------------------------------
def _make_drivers(db: Session, seed: int) -> None:
    rng = rng_for(seed, "drivers")
    nationalities = list(NATIONALITIES)
    weights = [NATIONALITIES[n][0] for n in nationalities]
    used_names: set[str] = set()
    year_start = date(SIM_YEAR, 1, 1)

    for i in range(1, DRIVER_COUNT + 1):
        nationality = rng.choices(nationalities, weights)[0]
        _, firsts, lasts = NATIONALITIES[nationality]
        name = f"{rng.choice(firsts)} {rng.choice(lasts)}"
        while name in used_names:  # keep names unique
            name = f"{rng.choice(firsts)} {rng.choice('ABCDEFGHJKLMNPRST')}. {rng.choice(lasts)}"
        used_names.add(name)

        if rng.random() < NEW_HIRE_SHARE:
            hire_date = date(SIM_YEAR, 1, 15) + timedelta(days=rng.randint(0, 289))  # Jan 15 to Oct 31
        else:
            hire_date = year_start - timedelta(days=rng.randint(1, 3650))
        db.add(Driver(
            employee_code=f"DRV-{i:04d}",
            name=name,
            nationality=nationality,
            shift=rng.choices(list(SHIFT_WEIGHTS), list(SHIFT_WEIGHTS.values()))[0],
            depot=rng.choice(DEPOTS),
            hire_date=hire_date,
            is_active=hire_date < year_start,
        ))
    db.flush()


def _make_trainers(db: Session, seed: int) -> None:
    rng = rng_for(seed, "trainers")
    for name in TRAINER_NAMES:
        db.add(Trainer(name=name, max_sessions_per_week=rng.randint(4, 6)))
    db.flush()


def _make_courses_and_targets(db: Session, seed: int) -> dict[str, int]:
    """Create courses and 2026 targets. Returns {course code: target}."""
    rng = rng_for(seed, "targets")
    active = db.scalar(select(func.count()).select_from(Driver).where(Driver.is_active.is_(True)))
    targets: dict[str, int] = {}
    for profile in COURSES:
        course = Course(
            code=profile.code, name=profile.name, duration_hours=profile.duration_hours,
            default_capacity=profile.capacity, is_mandatory=profile.mandatory,
        )
        db.add(course)
        db.flush()
        target = round(active * rng.uniform(*profile.target_share))
        db.add(TrainingTarget(course_id=course.id, year=SIM_YEAR, target_completions=target))
        targets[profile.code] = target
    db.flush()
    return targets


# --- Unavailability ---------------------------------------------------------------------
def _make_unavailability(db: Session, seed: int) -> None:
    """Annual leave (2-4 weeks each, mostly in Jul-Aug) plus a few known one-off absences."""
    rng = rng_for(seed, "unavailability")
    drivers = list(db.scalars(select(Driver).order_by(Driver.id)))
    year_end = date(SIM_YEAR, 12, 31)

    def add(driver: Driver, start: date, days: int, reason: str) -> None:
        if start < driver.hire_date or start > year_end:
            return
        db.add(DriverUnavailability(
            driver_id=driver.id,
            start_time=datetime.combine(start, time.min),
            end_time=datetime.combine(start + timedelta(days=days), time.min),
            reason=reason,
        ))

    for driver in drivers:
        if rng.random() < 0.65:  # summer cluster
            start = date(SIM_YEAR, 6, 15) + timedelta(days=rng.randint(0, 60))
        else:
            start = date(SIM_YEAR, 1, 1) + timedelta(days=rng.randint(0, 330))
        add(driver, start, rng.randint(14, 28), "annual leave")
        if rng.random() < 0.12:  # a few take a second, off-peak block
            add(driver, date(SIM_YEAR, 10, 1) + timedelta(days=rng.randint(0, 60)), rng.randint(14, 21), "annual leave")

    for driver in rng.sample(drivers, 20):  # known absences
        start = date(SIM_YEAR, 1, 1) + timedelta(days=rng.randint(0, 350))
        add(driver, start, rng.randint(1, 3), rng.choice(UNAVAILABILITY_REASONS))
    db.flush()


# --- Sessions ---------------------------------------------------------------------------
def _make_sessions(db: Session, seed: int, targets: dict[str, int]) -> None:
    """Spread each course's sessions over the year, respecting trainer load and double booking."""
    rng = rng_for(seed, "sessions")
    trainers = list(db.scalars(select(Trainer).order_by(Trainer.id)))
    courses = {c.code: c for c in db.scalars(select(Course))}
    busy: dict[int, list[tuple[datetime, datetime]]] = defaultdict(list)  # trainer -> time ranges
    weekly_load: dict[tuple[int, date], int] = defaultdict(int)  # (trainer, week start) -> sessions
    year_end = date(SIM_YEAR, 12, 31)

    def trainer_can_take(trainer: Trainer, start: datetime, end: datetime) -> bool:
        if weekly_load[(trainer.id, week_start(start.date()))] >= trainer.max_sessions_per_week:
            return False
        return not any(start < b_end and end > b_start for b_start, b_end in busy[trainer.id])

    for profile in COURSES:
        course = courses[profile.code]
        planned_seats = targets[profile.code] * profile.capacity_factor
        n_sessions = math.ceil(planned_seats / profile.capacity)
        weights = scenarios.SLOT_WEIGHTS_BY_COURSE.get(profile.code, {h: 1 for h in SESSION_START_HOURS})
        spacing = 365 / n_sessions

        for k in range(n_sessions):
            day = date(SIM_YEAR, 1, 1) + timedelta(days=max(0, round(k * spacing + rng.randint(-2, 2))))
            hour = rng.choices(list(weights), list(weights.values()))[0]
            # Try this slot, then keep moving forward until some trainer can take it.
            while day <= year_end:
                if day.weekday() in SESSION_WEEKDAYS:
                    start = datetime.combine(day, time(hour))
                    end = session_end(start, course.duration_hours)
                    candidates = list(trainers)
                    rng.shuffle(candidates)
                    preferred = profile.preferred_trainer_id
                    if preferred is not None and rng.random() < 0.85:
                        candidates.sort(key=lambda t: t.id != preferred)  # preferred first, rest stay shuffled
                    chosen = next((t for t in candidates if trainer_can_take(t, start, end)), None)
                    if chosen:
                        db.add(TrainingSession(
                            course_id=course.id, trainer_id=chosen.id, start_time=start, end_time=end,
                            location=rng.choice(DEPOTS).replace("Depot", "Training Room"),
                            capacity=profile.capacity, status="scheduled", source="seed",
                        ))
                        busy[chosen.id].append((start, end))
                        weekly_load[(chosen.id, week_start(day))] += 1
                        break
                day += timedelta(days=1)
    db.flush()


# --- Entry points -----------------------------------------------------------------------
def table_counts(db: Session) -> dict[str, int]:
    return {t.name: db.scalar(select(func.count()).select_from(t)) for t in Base.metadata.sorted_tables}


def generate(db: Session, seed: int) -> dict[str, int]:
    """Fill an empty (freshly created) database. Same seed gives identical data."""
    _make_drivers(db, seed)
    _make_trainers(db, seed)
    targets = _make_courses_and_targets(db, seed)
    _make_unavailability(db, seed)
    _make_sessions(db, seed, targets)
    db.add(SimState(id=1, current_time=SIM_START))
    db.flush()
    book_window(db, seed, SIM_START)  # enrollments only for the first 21 days
    counts = table_counts(db)
    log_event(db, SIM_START, "system", "seed_created", "seed", None, {"seed": seed, "row_counts": counts})
    db.commit()
    create_views(db)  # readable v_* views for browsing the data
    return table_counts(db)


def reseed(engine: Engine, seed: int) -> dict[str, int]:
    """Drop and recreate every table, then generate. Used by --reset, the API and the report."""
    drop_views(engine)  # views must go first (Postgres will not drop tables they depend on)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False, autoflush=False) as db:
        return generate(db, seed)


def main() -> None:
    from app.database import engine

    parser = argparse.ArgumentParser(description="Generate one synthetic training year.")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--reset", action="store_true", help="drop and recreate all tables first")
    args = parser.parse_args()

    if args.reset:
        counts = reseed(engine, args.seed)
    else:
        Base.metadata.create_all(engine)
        with Session(engine, expire_on_commit=False, autoflush=False) as db:
            if db.scalar(select(func.count()).select_from(Driver)):
                raise SystemExit("Database already has data. Use --reset to wipe and reseed.")
            counts = generate(db, args.seed)
    print(f"Seeded with seed={args.seed}")
    for table, n in counts.items():
        print(f"  {table:24s}{n}")


if __name__ == "__main__":
    main()
