"""A deterministic, rule-based planner. No LLM. It gives every planner run one honest baseline plan.

What it does (in this order, stopping as soon as the expected gain closes the gap):
1. Fill free seats in existing upcoming sessions, emptiest sessions first, using only drivers who pass the booking rules
   at that session's exact time.
2. If a gap remains, add up to ``MAX_NEW_SESSIONS`` new sessions: the window (Sun-Thu 09/14/18, after the lead time) where the
   most not-yet-used drivers pass, with a free trainer, then fill it the same way.

Which driver comes first: the one with the best predicted show-up rate, with a penalty for groups (shift, nationality) that
already carry more than their share of the eligible pool, so the burden is spread. The plan goes through the same
``check_plan`` as every other plan; nothing here is trusted.
"""
from datetime import timedelta

from sqlalchemy.orm import Session

from app.config import settings
from app.models import Alert, Course, Driver
from app.schemas.plan import MAX_ACTIONS
from app.services.constraints import SessionInfo, driver_can_book, load_snapshot_for_ids, trainer_can_run
from app.services.feasibility import DEFAULT_LOCATION, Feasibility
from app.services.planner_policy import earliest_new_session_start
from app.services.ranking import ScoringContext, show_probability
from app.schemas.plan import AddSession
from app.simulator.rules import session_end

MAX_NEW_SESSIONS = 2
CANDIDATE_WINDOWS_TO_TRY = 6
SEARCH_DAYS = 28
FAIRNESS_PENALTY = 0.5  # how strongly a group over its pool share is pushed down the order
CONFIDENCE_Z = 1.28  # aim for about a 90% chance of hitting the target, not just "expected completions reach it"


class _Picker:
    """Chooses the next driver, balancing predicted show-up against how much each group has already been given."""

    def __init__(self, ctx: ScoringContext, course_id: int):
        self.ctx, self.course_id = ctx, course_id
        self.assigned = 0
        self.counts: dict[tuple[str, str], int] = {}
        pool = len(ctx.pool) or 1
        self.share = {}
        for field in ("shift", "nationality"):
            for d in ctx.pool:
                self.share[(field, getattr(d, field))] = self.share.get((field, getattr(d, field)), 0) + 1 / pool

    def score(self, driver: Driver) -> float:
        fact = self.ctx.drivers.get(driver.id)
        excess = 0.0
        if fact is not None:
            for field in ("shift", "nationality"):
                key = (field, getattr(fact, field))
                after = (self.counts.get(key, 0) + 1) / (self.assigned + 1)
                excess = max(excess, after - self.share.get(key, 0.0))
        return show_probability(self.ctx, self.course_id, driver.id) - FAIRNESS_PENALTY * excess

    def best(self, candidates: list[Driver]) -> Driver:
        return max(candidates, key=lambda d: (round(self.score(d), 6), -d.id))

    def took(self, driver: Driver) -> None:
        self.assigned += 1
        fact = self.ctx.drivers.get(driver.id)
        if fact is not None:
            for field in ("shift", "nationality"):
                key = (field, getattr(fact, field))
                self.counts[key] = self.counts.get(key, 0) + 1


def build_greedy_plan(db: Session, alert: Alert, ctx: ScoringContext, feas: Feasibility | None = None) -> dict | None:
    """Return a plan dict (course_id, alert_id, rationale, actions), or None when there is nothing to do."""
    now = ctx.now
    course: Course = db.get(Course, alert.course_id)
    feas = feas or Feasibility(db, course, now)
    # The goal is to reach the target with some confidence. An attendance_gap alert can have expected completions at or
    # above the target but a low chance of hitting it, so the goal includes a safety margin of CONFIDENCE_Z standard deviations.
    fc = ctx.fc
    gap = max(ctx.gap, fc.target - fc.attended - fc.mu + CONFIDENCE_Z * fc.sigma)
    if gap <= 1e-9 or not feas.pool:
        return None

    existing = feas.sessions_with_free_seats()
    snap = load_snapshot_for_ids(db, course.id, now, {d.id for d in feas.pool}, {s.id for s, _ in existing},
                                 {t.id for t in feas.trainers})
    picker = _Picker(ctx, course.id)
    actions: list[dict] = []
    gain = 0.0
    used: set[int] = set()
    notes: list[str] = []

    def fill(info: SessionInfo, session_ref, seats: int) -> int:
        """Book up to ``seats`` drivers into one session. Returns how many were booked."""
        nonlocal gain
        booked = 0
        while booked < seats and gain < gap and len(actions) < MAX_ACTIONS - 1:
            candidates = [d for d in feas.pool if d.id not in used and not driver_can_book(snap, d, info)]
            if not candidates:
                break
            driver = picker.best(candidates)
            snap.book(driver.id, info.key)
            used.add(driver.id)
            picker.took(driver)
            gain += show_probability(ctx, course.id, driver.id)
            actions.append({"type": "enroll_driver", "driver_id": driver.id, "session_id": session_ref})
            booked += 1
        return booked

    # 1. Free seats in existing sessions, emptiest first.
    for session, free in sorted(existing, key=lambda item: (-item[1] / max(1, item[0].capacity), item[0].start_time, item[0].id)):
        if gain >= gap:
            break
        n = fill(snap.sessions[session.id], session.id, free)
        if n:
            notes.append(f"{n} driver(s) into session {session.id}")

    # 2. New sessions for whatever gap is left.
    first_day = earliest_new_session_start(now).date()
    for number in range(1, MAX_NEW_SESSIONS + 1):
        if gain >= gap or len(actions) >= MAX_ACTIONS - 2:
            break
        windows = [w for w in feas.windows(first_day, first_day + timedelta(days=SEARCH_DAYS)) if w.free_trainers]
        windows.sort(key=lambda w: (-w.count, w.start))
        best = None  # (usable driver count, window)
        for window in windows[:CANDIDATE_WINDOWS_TO_TRY]:
            info = SessionInfo(f"new:{number}", course.id, 0, window.start, window.end, course.default_capacity)
            usable = sum(1 for d in feas.pool if d.id not in used and not driver_can_book(snap, d, info))
            if best is None or usable > best[0]:
                best = (usable, window)
        if best is None or best[0] < settings.planner_min_new_session_size:
            break
        window = best[1]
        trainer_id = None
        for tid in window.free_trainers:
            action = AddSession(type="add_session", temp_ref=f"new:{number}", course_id=course.id, trainer_id=tid,
                                start=window.start, end=window.end, location=DEFAULT_LOCATION, capacity=course.default_capacity)
            if not trainer_can_run(snap, action):
                trainer_id = tid
                break
        if trainer_id is None:
            break
        ref = f"new:{number}"
        info = SessionInfo(ref, course.id, trainer_id, window.start, window.end, course.default_capacity)
        snap.add_session(info)
        actions.append({
            "type": "add_session", "temp_ref": ref, "course_id": course.id, "trainer_id": trainer_id,
            "start": window.start.isoformat(), "end": window.end.isoformat(), "location": DEFAULT_LOCATION,
            "capacity": course.default_capacity,
        })
        n = fill(info, ref, course.default_capacity)
        notes.append(f"new session {ref} on {window.start:%a %d %b %H:%M} with {n} driver(s)")
        if n < settings.planner_min_new_session_size:  # not worth a session: take it back out
            actions[:] = [a for a in actions if a.get("temp_ref") != ref and a.get("session_id") != ref]
            break

    if not any(a["type"] == "enroll_driver" for a in actions):
        return None
    return {
        "course_id": course.id, "alert_id": alert.id,
        "rationale": "Rule-based baseline: " + "; ".join(notes) + f". Expected extra completions about {gain:.1f} against a gap of {gap:.1f}.",
        "actions": actions,
    }
