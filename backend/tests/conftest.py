import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.simulator.seed import reseed

SEED = 42


def make_seeded_engine(seed: int = SEED):
    """A brand-new in-memory SQLite database, already seeded."""
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    reseed(engine, seed)
    return engine


@pytest.fixture()
def engine():
    return make_seeded_engine()


@pytest.fixture()
def db(engine):
    with Session(engine, expire_on_commit=False, autoflush=False) as session:
        yield session


@pytest.fixture()
def client(engine):
    """A TestClient wired to the seeded in-memory DB (no `with`, so the real training.db is never touched)."""
    from fastapi.testclient import TestClient

    from app.database import get_db
    from app.main import app

    def override_db():
        with Session(engine, expire_on_commit=False, autoflush=False) as session:
            yield session

    app.dependency_overrides[get_db] = override_db
    yield TestClient(app)
    app.dependency_overrides.clear()


# --- Phase 5: a seeded year played to week 20, so open alerts exist -----------------------------------
def clone_engine(source):
    """A private in-memory copy of a database, so tests that write (plans, runs, audit rows) never affect each other."""
    copy = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    src, dst = source.raw_connection(), copy.raw_connection()
    src.driver_connection.backup(dst.driver_connection)
    return copy


@pytest.fixture(scope="session")
def alert_template():
    """Seed 42 played 140 days (to 2026-05-21). Alert 1 is a capacity_gap, alert 6 an attendance_gap. Never modify it."""
    from app.simulator.engine import advance

    engine = make_seeded_engine()
    with Session(engine, expire_on_commit=False, autoflush=False) as db:
        advance(db, 140)
    return engine


@pytest.fixture()
def aengine(alert_template):
    return clone_engine(alert_template)


@pytest.fixture()
def adb(aengine):
    with Session(aengine, expire_on_commit=False, autoflush=False) as session:
        yield session


@pytest.fixture(autouse=True)
def no_planner_pause(monkeypatch):
    """The real pause between planner samples would only slow the tests down."""
    monkeypatch.setattr("app.config.settings.planner_sample_pause_seconds", 0)


@pytest.fixture(autouse=True)
def planner_defaults(monkeypatch):
    """Most planner tests script exactly the LLM replies they need, so the rule-based baseline is off unless a test turns it on."""
    monkeypatch.setattr("app.config.settings.planner_greedy_baseline", False)
