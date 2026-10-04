"""Year-end forecast per course: "pipeline forecast with smoothed rates". Plain Python + ``math`` only.

We already know the future schedule, so we do not extrapolate a curve blindly. Instead we
forecast the two rates that matter and apply them to the seats still on the calendar:

* ``p_show``: the share of booked people who turn up (attended ÷ (attended + no-show));
* ``p_fill``: the share of seats that get used in sessions that are not booked yet (seats used ÷ capacity).

Both are smoothed over the weeks so far with an exponentially weighted moving average
(``ALPHA``) so that recent weeks matter more, and then shrunk toward the fleet-wide rate
(see ``shrunk_rate``). Then, for every remaining scheduled session:

* inside the booking window (people are already booked):  n = booked,   q = p_show
* beyond the window (nobody booked yet):                  n = capacity, q = p_fill × p_show

Each of the n people turns up with probability q, so the session adds on average n·q completions
with variance n·q·(1−q). Add them up over all remaining sessions to get μ and σ².
"""
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from app.services.tracking import (
    CourseFact, TrackingFacts, attended_total, completed_by_week, completed_driver_ids, eligible_pool, series_from_facts,
    target_pace, week_end, week_starts, year_fraction,
)
from app.simulator.rules import BOOKING_WINDOW_DAYS, SIM_END, week_start

# --- Tunable constants (every one is explained in docs/forecast_backtest.md) --------------
ALPHA = 0.3  # EWMA smoothing: a week's weight is (1 - ALPHA) ** (weeks ago). Higher = reacts faster, noisier
K = 20  # pseudo-count for shrinkage: "20 imaginary observations at the fleet rate"
PRIOR_SHOW = 0.85  # show-up rate assumed before any data exists
PRIOR_FILL = 0.85  # fill rate assumed before any data exists
INFLATION = 6.0  # multiplies the variance; tuned by the backtest (docs/forecast_backtest.md) so the 90% range covers ~90%
Z90 = 1.645  # normal quantile for a 90% two-sided range


# --- Small maths helpers ------------------------------------------------------------------
def normal_cdf(x: float) -> float:
    """Φ(x), the standard normal CDF, via math.erf."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def ewma_counts(weekly: list[tuple[float, float]], alpha: float = ALPHA) -> tuple[float, float]:
    """Exponentially weighted (hits, trials) over weeks, oldest first.

    The newest week has weight 1, the one before (1 − alpha), then (1 − alpha)², and so on. Weeks with no
    sessions have zero hits and trials, so they only make older weeks count for relatively less.
    """
    hits = trials = 0.0
    n = len(weekly)
    for i, (h, t) in enumerate(weekly):
        weight = (1.0 - alpha) ** (n - 1 - i)
        hits += weight * h
        trials += weight * t
    return hits, trials


def shrunk_rate(hits: float, trials: float, fleet_rate: float, k: float = K) -> float:
    """Blend a course's own rate with the fleet-wide rate so small samples are not trusted too much.

    rate = (hits + k × fleet_rate) / (trials + k)

    In plain words: pretend we also saw ``k`` extra observations that all happened at the fleet rate.
    A course with few observations (early in the year, or a small course like EV) stays close to the fleet
    rate; as real observations pile up, the course's own rate takes over. (Statisticians call this a Beta prior.)
    """
    return (hits + k * fleet_rate) / (trials + k)


def smoothed_rate(weekly: list[tuple[float, float]], fallback: float, k: float = K, alpha: float = ALPHA) -> float:
    """Recent-weighted rate for one course, shrunk toward ``fallback`` (the fleet rate, or the prior).

    ``weekly`` is [(hits, trials), ...] oldest week first. Step 1: the EWMA gives the course's recent rate
    (weighted hits ÷ weighted trials). Step 2: that rate stands for all the course's ``n`` real observations
    (n = total trials so far), so the shrinkage formula is applied with hits = n × recent rate.
    """
    n = sum(trials for _hits, trials in weekly)
    hits_w, trials_w = ewma_counts(weekly, alpha)
    recent = hits_w / trials_w if trials_w > 0 else fallback
    return shrunk_rate(n * recent, n, fallback, k)


# --- Pipeline arithmetic (pure: tiny hand-built inputs work in tests) ---------------------
@dataclass
class SessionTerm:
    """What one remaining session is expected to contribute."""

    start_time: datetime
    n: float  # people who could attend
    q: float  # chance each one does
    in_window: bool

    @property
    def mean(self) -> float:
        return self.n * self.q

    @property
    def variance(self) -> float:
        return self.n * self.q * (1.0 - self.q)


def session_terms(
    remaining: list, now: datetime, p_show: float, p_fill: float,
) -> list[SessionTerm]:
    """Turn the remaining scheduled sessions (objects with ``start_time``, ``capacity``, ``booked``) into terms."""
    window_end = now + timedelta(days=BOOKING_WINDOW_DAYS)
    terms = []
    for s in remaining:
        if s.start_time < window_end:
            terms.append(SessionTerm(s.start_time, float(s.booked), p_show, True))
        else:
            terms.append(SessionTerm(s.start_time, float(s.capacity), p_fill * p_show, False))
    return terms


@dataclass
class Projection:
    mu: float
    sigma: float
    projected: float
    low: float
    high: float
    p_hit: float


def project(attended: int, target: int, pool: int, terms: list[SessionTerm], inflation: float | None = None) -> Projection:
    """Year-end projection, 90% range and P(hit target) from the remaining sessions.

    - μ = Σ n·q, σ² = inflation × Σ n·q·(1 − q);
    - projection = attended + μ, never more than attended + pool;
    - 90% range = projection ± 1.645σ, clipped to [attended, attended + pool];
    - P(hit) = 1 − Φ((target − attended − 0.5 − μ) / σ); with σ = 0 it is simply 0 or 1.
    """
    inflation = INFLATION if inflation is None else inflation
    mu = sum(t.mean for t in terms)
    sigma = math.sqrt(inflation * sum(t.variance for t in terms))
    ceiling = attended + pool
    projected = min(attended + mu, ceiling)
    low = max(attended, projected - Z90 * sigma)
    high = min(ceiling, projected + Z90 * sigma)

    need = target - attended
    if need <= 0:
        p_hit = 1.0
    elif need > pool:
        p_hit = 0.0  # not enough eligible drivers left, whatever the schedule says
    elif sigma == 0:
        p_hit = 1.0 if mu >= need else 0.0
    else:
        p_hit = 1.0 - normal_cdf((need - 0.5 - mu) / sigma)
    return Projection(mu, sigma, projected, low, high, p_hit)


def forecast_cone(
    attended: int, pool: int, terms: list[SessionTerm], now: datetime, inflation: float | None = None,
) -> list[dict]:
    """Per future week: forecast mean and 90% range of cumulative completions at the end of that week.

    Each session's mean and variance go into the week it starts in; weeks accumulate. The last point
    equals the year-end projection.
    """
    inflation = INFLATION if inflation is None else inflation
    by_week_mean: dict[date, float] = {}
    by_week_var: dict[date, float] = {}
    for t in terms:
        ws = week_start(t.start_time.date())
        by_week_mean[ws] = by_week_mean.get(ws, 0.0) + t.mean
        by_week_var[ws] = by_week_var.get(ws, 0.0) + t.variance

    ceiling = attended + pool
    cum_mean = cum_var = 0.0
    points = []
    for ws in week_starts():
        end = week_end(ws)
        if end <= now:
            continue
        cum_mean += by_week_mean.get(ws, 0.0)
        cum_var += by_week_var.get(ws, 0.0)
        mean = min(attended + cum_mean, ceiling)
        sd = math.sqrt(inflation * cum_var)
        points.append({
            "as_of": end,
            "forecast_mean": mean,
            "forecast_low": max(attended, mean - Z90 * sd),
            "forecast_high": min(ceiling, mean + Z90 * sd),
        })
    return points


# --- Baselines (backtest and comparison only, never used for flags) ------------------------
def naive_run_rate(closed_weekly_attended: list[int], attended: int, now: datetime, alpha: float = ALPHA) -> float:
    """Pure time-series answer that ignores the calendar: attended + EWMA of weekly completions × weeks remaining."""
    if not closed_weekly_attended:
        return float(attended)
    level = float(closed_weekly_attended[0])
    for x in closed_weekly_attended[1:]:
        level = alpha * x + (1 - alpha) * level
    weeks_remaining = max(0.0, (SIM_END - now).total_seconds() / (7 * 86400))
    return attended + level * weeks_remaining


def linear_extrapolation(attended: int, now: datetime) -> float:
    """attended ÷ elapsed fraction of the year."""
    fraction = year_fraction(now)
    return attended / fraction if fraction > 0 else float(attended)


# --- Per-course result --------------------------------------------------------------------
@dataclass
class CourseForecast:
    course_id: int
    code: str
    name: str
    is_mandatory: bool
    target: int
    attended: int
    target_pace: float
    pace_gap: float
    p_show: float
    p_fill: float
    fleet_show: float
    fleet_fill: float
    remaining_sessions: int
    remaining_seats: int
    expected_attempts: float  # people we expect to put in seats: Σ booked (in window) + Σ capacity × p_fill (beyond)
    eligible_pool: int
    mu: float
    sigma: float
    projected: float
    low: float
    high: float
    p_hit: float
    naive_projection: float
    linear_projection: float
    weeks_observed: int  # weeks closed so far this year
    completed_sessions: int
    # Filled in by services.risk.assess
    risk_level: str = "insufficient_data"
    shortfall_type: str = "none"
    seats_needed: float = 0.0
    reasons: list[dict] = field(default_factory=list)


@dataclass
class FleetRates:
    show: float
    fill: float


def fleet_rates(facts: TrackingFacts) -> FleetRates:
    """Show and fill rates pooled over every course (same smoothing, shrunk toward the priors)."""
    weekly_show: dict[date, list[float]] = {}
    weekly_fill: dict[date, list[float]] = {}
    for course in facts.courses:
        for ws, (att, ns, used, cap) in completed_by_week(facts, course.id).items():
            show = weekly_show.setdefault(ws, [0.0, 0.0])
            show[0] += att
            show[1] += att + ns
            fill = weekly_fill.setdefault(ws, [0.0, 0.0])
            fill[0] += used
            fill[1] += cap
    return FleetRates(
        show=smoothed_rate(_weekly_list(facts.now, weekly_show), PRIOR_SHOW),
        fill=smoothed_rate(_weekly_list(facts.now, weekly_fill), PRIOR_FILL),
    )


def _weekly_list(now: datetime, weekly: dict[date, list[float]]) -> list[tuple[float, float]]:
    """[(hits, trials)] for every week that has started by ``now``, oldest first (empty weeks are (0, 0))."""
    return [
        (weekly[ws][0], weekly[ws][1]) if ws in weekly else (0.0, 0.0)
        for ws in week_starts()
        if datetime.combine(ws, datetime.min.time()) < now
    ]


def forecast_course(facts: TrackingFacts, course: CourseFact, fleet: FleetRates, inflation: float | None = None) -> CourseForecast:
    """Everything the dashboard shows for one course, except the risk fields."""
    now = facts.now
    weekly = completed_by_week(facts, course.id)
    show_series = {ws: [att, att + ns] for ws, (att, ns, _u, _c) in weekly.items()}
    fill_series = {ws: [used, cap] for ws, (_a, _n, used, cap) in weekly.items()}
    p_show = smoothed_rate(_weekly_list(now, show_series), fleet.show)
    p_fill = smoothed_rate(_weekly_list(now, fill_series), fleet.fill)

    attended = attended_total(facts, course.id)
    pool = eligible_pool(facts.drivers, completed_driver_ids(facts, course.id), now)
    remaining = [s for s in facts.sessions_of(course.id) if s.status == "scheduled" and s.start_time >= now]
    terms = session_terms(remaining, now, p_show, p_fill)
    result = project(attended, course.target, pool, terms, inflation)

    series = series_from_facts(facts, course)
    closed = [row["attended_in_week"] for row in series if row["closed"]]
    pace = target_pace(course.target, now)
    return CourseForecast(
        course_id=course.id, code=course.code, name=course.name, is_mandatory=course.is_mandatory,
        target=course.target, attended=attended, target_pace=round(pace, 2), pace_gap=round(attended - pace, 2),
        p_show=p_show, p_fill=p_fill, fleet_show=fleet.show, fleet_fill=fleet.fill,
        remaining_sessions=len(remaining), remaining_seats=sum(s.capacity for s in remaining),
        expected_attempts=sum(t.n if t.in_window else t.n * p_fill for t in terms),
        eligible_pool=pool, mu=result.mu, sigma=result.sigma, projected=result.projected,
        low=result.low, high=result.high, p_hit=result.p_hit,
        naive_projection=naive_run_rate(closed, attended, now),
        linear_projection=linear_extrapolation(attended, now),
        weeks_observed=len(closed),
        completed_sessions=sum(1 for s in facts.sessions_of(course.id) if s.status == "completed"),
    )


def course_cone(facts: TrackingFacts, course: CourseFact, fc: CourseForecast, inflation: float | None = None) -> list[dict]:
    """The forecast cone for the chart (needs the same terms as ``forecast_course``)."""
    remaining = [s for s in facts.sessions_of(course.id) if s.status == "scheduled" and s.start_time >= facts.now]
    terms = session_terms(remaining, facts.now, fc.p_show, fc.p_fill)
    return forecast_cone(fc.attended, fc.eligible_pool, terms, facts.now, inflation)
