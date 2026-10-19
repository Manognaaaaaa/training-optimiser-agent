"""Scores and ranks the valid plans of a planner run. Pure Python, no LLM.

A plan is scored on five components, each 0 to 1 (higher is better), then combined with fixed weights
(``settings.score_weight_*``). The LLM never decides the order; this module does, and the same inputs
always give the same order.

| component  | question it answers                                                          |
|------------|------------------------------------------------------------------------------|
| closure    | how much of the expected shortfall does the plan close?                     |
| confidence | how likely is it that the course hits its target once the plan is applied?  |
| fairness   | is the burden (drivers moved or newly booked) spread like the eligible pool?|
| disruption | how many drivers and sessions does the plan touch?                          |
| resource   | how many extra trainer hours does it need?                                  |

What-if forecast: the Phase 3 forecast gives the expected completions mu = sum(n*q) over the remaining
sessions. Applying a plan only adds seats (enrol, move target) and removes seats (move source), each worth
that driver's own chance of showing up, so mu_after = mu_before + sum(q added) - sum(q removed), and the
variance changes the same way. ``forecasting.project_moments`` turns that into P(hit). A move therefore
changes nothing by itself (the same driver leaves one seat and takes another); the gain comes from new seats.
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Alert, Plan, PlannerRun
from app.schemas.plan import AddSession, EnrollDriver, MoveDriver, Violation, parse_plan
from app.services.audit import log_event
from app.services.forecasting import CourseForecast, fleet_rates, forecast_course, project_moments, session_terms, shrunk_rate
from app.services.sim_clock import get_sim_now
from app.services.tracking import DriverFact, TrackingFacts, completed_driver_ids, eligible_drivers, load_facts

SCORING_VERSION = "v1"
GROUPINGS = ("shift", "nationality")


def weights() -> dict[str, float]:
    """The weights in use right now (they sum to 1; config refuses to start otherwise)."""
    return {
        "closure": settings.score_weight_closure, "confidence": settings.score_weight_confidence,
        "fairness": settings.score_weight_fairness, "disruption": settings.score_weight_disruption,
        "resource": settings.score_weight_resource,
    }


@dataclass
class ScoringContext:
    """Everything about the alert's course that does not depend on the plan, computed once per run."""

    now: datetime
    facts: TrackingFacts
    fc: CourseForecast
    variance: float  # sum of n*q*(1-q) over the remaining sessions, before any plan
    pool: list[DriverFact]  # the drivers who could still complete the course
    drivers: dict[int, DriverFact]

    @property
    def gap(self) -> float:
        """Completions we still expect to be short by: target - attended - mu (never negative)."""
        return max(0.0, self.fc.target - self.fc.attended - self.fc.mu)


def build_context(db: Session, alert: Alert, now: datetime | None = None) -> ScoringContext:
    facts = load_facts(db)
    if now is not None:
        facts.now = now
    course = next(c for c in facts.courses if c.id == alert.course_id)
    fc = forecast_course(facts, course, fleet_rates(facts))
    remaining = [s for s in facts.sessions_of(course.id) if s.status == "scheduled" and s.start_time >= facts.now]
    variance = sum(t.variance for t in session_terms(remaining, facts.now, fc.p_show, fc.p_fill))
    pool = eligible_drivers(facts.drivers, completed_driver_ids(facts, course.id), facts.now)
    return ScoringContext(facts.now, facts, fc, variance, pool, {d.id: d for d in facts.drivers})


def _share(counts: dict[str, int]) -> dict[str, float]:
    total = sum(counts.values())
    return {g: round(n / total, 4) for g, n in sorted(counts.items())} if total else {}


def _group_counts(drivers: list[DriverFact], field: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for d in drivers:
        value = getattr(d, field)
        counts[value] = counts.get(value, 0) + 1
    return counts


def score_plan(ctx: ScoringContext, course_id: int, actions: list[dict]) -> dict[str, Any]:
    """Score one valid plan. Returns the breakdown dict stored in ``plans.score_breakdown`` (``score_total`` inside)."""
    parsed = parse_plan({"course_id": course_id, "actions": actions})
    if isinstance(parsed, Violation):
        raise ValueError(f"cannot score a plan that does not parse: {parsed.message}")
    fc, facts = ctx.fc, ctx.facts
    w = weights()

    # Which sessions belong to this course (existing ones from the facts, new ones from the plan itself).
    new_course = {a.temp_ref: a.course_id for a in parsed.actions if isinstance(a, AddSession)}
    existing_course = {s.id: s.course_id for s in facts.sessions}

    def in_course(ref) -> bool:
        return new_course.get(ref) == course_id if isinstance(ref, str) else existing_course.get(ref) == course_id

    def q_of(driver_id: int) -> float:
        """The driver's own show-up chance: their record on this course, shrunk toward the course rate."""
        att, no_show = facts.driver_course.get((course_id, driver_id), (0, 0))
        return shrunk_rate(att, att + no_show, fc.p_show) if att + no_show else fc.p_show

    d_mu = d_var = 0.0
    touched: dict[int, DriverFact] = {}
    sessions_added = 0
    trainer_hours = 0.0
    for a in parsed.actions:
        if isinstance(a, AddSession):
            sessions_added += 1
            trainer_hours += (a.end - a.start).total_seconds() / 3600
        elif isinstance(a, EnrollDriver):
            q = q_of(a.driver_id)
            if in_course(a.session_id):
                d_mu += q
                d_var += q * (1 - q)
            if a.driver_id in ctx.drivers:
                touched[a.driver_id] = ctx.drivers[a.driver_id]
        elif isinstance(a, MoveDriver):
            q = q_of(a.driver_id)
            if in_course(a.to_session_id):
                d_mu += q
                d_var += q * (1 - q)
            if in_course(a.from_session_id):
                d_mu -= q
                d_var -= q * (1 - q)
            if a.driver_id in ctx.drivers:
                touched[a.driver_id] = ctx.drivers[a.driver_id]

    mu_after = fc.mu + d_mu
    after = project_moments(fc.attended, fc.target, fc.eligible_pool, mu_after, ctx.variance + d_var)
    expected_gain = d_mu
    gap = ctx.gap
    closure = 1.0 if gap <= 1e-9 else min(1.0, max(0.0, expected_gain) / gap)
    confidence = after.p_hit

    # Fairness: each group's share of the burden minus its share of the eligible pool; the worst excess counts.
    burden = list(touched.values())
    max_excess = 0.0
    flag = False
    by_grouping: dict[str, dict] = {}
    for field in GROUPINGS:
        burden_share = _share(_group_counts(burden, field))
        pool_share = _share(_group_counts(ctx.pool, field))
        by_grouping[field] = {"burden": burden_share, "pool": pool_share}
        for group, share in burden_share.items():
            max_excess = max(max_excess, share - pool_share.get(group, 0.0))
            if share > settings.fairness_flag_burden and pool_share.get(group, 0.0) < settings.fairness_flag_pool:
                flag = True
    fairness = max(0.0, 1.0 - max_excess)

    drivers_moved = len(touched)
    disruption = 1.0 - min(1.0, (drivers_moved + 2 * sessions_added) / settings.disruption_cap)
    resource = 1.0 - min(1.0, trainer_hours / settings.trainer_hours_cap)

    components = {"closure": closure, "confidence": confidence, "fairness": fairness, "disruption": disruption, "resource": resource}
    weighted = {k: round(w[k] * v, 4) for k, v in components.items()}
    total = round(sum(w[k] * v for k, v in components.items()), 4)
    return {
        "scoring_version": SCORING_VERSION, "score_total": total,
        "weights": w, "components": {k: round(v, 4) for k, v in components.items()}, "weighted": weighted,
        "p_hit_before": round(fc.p_hit, 4), "p_hit_after": round(after.p_hit, 4),
        "mu_before": round(fc.mu, 3), "mu_after": round(mu_after, 3),
        "expected_gain": round(expected_gain, 3), "shortfall_gap": round(gap, 3),
        "drivers_moved": drivers_moved, "sessions_added": sessions_added, "trainer_hours_added": round(trainer_hours, 2),
        "fairness_flag": flag, "fairness_max_excess": round(max_excess, 4), "fairness_groups": by_grouping,
    }


def sort_key(breakdown: dict, plan_hash: str | None) -> tuple:
    """Best first: higher total, then higher closure, then fewer drivers moved, then plan hash (so ties are stable)."""
    return (-breakdown["score_total"], -breakdown["components"]["closure"], breakdown["drivers_moved"], plan_hash or "")


def rankable_plans(db: Session, run_id: int) -> list[Plan]:
    """Valid, non-duplicate plans of one run, whose final checker verdict is clean."""
    return [
        p for p in db.scalars(select(Plan).where(Plan.run_id == run_id).order_by(Plan.sample_index))
        if p.is_valid and p.outcome == "valid" and not p.is_duplicate and not p.rejection_reasons
    ]


def rank_plans(db: Session, run_id: int) -> list[Plan]:
    """Score every valid plan of a run, store score and rank (1 = best) and write one audit row. Does not commit.

    Invalid plans are not touched. Returns the ranked plans, best first.
    """
    run = db.get(PlannerRun, run_id)
    alert = db.get(Alert, run.alert_id)
    now = get_sim_now(db)
    plans = rankable_plans(db, run_id)
    ranked: list[Plan] = []
    if plans:
        ctx = build_context(db, alert, now)
        scored = [(p, score_plan(ctx, alert.course_id, p.actions)) for p in plans]
        scored.sort(key=lambda item: sort_key(item[1], item[0].plan_hash))
        for rank, (plan, breakdown) in enumerate(scored, start=1):
            plan.rank, plan.score, plan.score_total = rank, breakdown["score_total"], breakdown["score_total"]
            plan.score_breakdown, plan.scoring_version, plan.status = breakdown, SCORING_VERSION, "valid"
            ranked.append(plan)
    log_event(db, now, "system", "plans_ranked", "planner_run", run_id, {
        "alert_id": alert.id, "scoring_version": SCORING_VERSION, "weights": weights(), "top_k": settings.top_k,
        "ranking": [
            {"rank": p.rank, "plan_id": p.id, "score_total": p.score_total, "components": p.score_breakdown["components"],
             "p_hit_before": p.score_breakdown["p_hit_before"], "p_hit_after": p.score_breakdown["p_hit_after"],
             "fairness_flag": p.score_breakdown["fairness_flag"]}
            for p in ranked
        ],
    })
    return ranked
