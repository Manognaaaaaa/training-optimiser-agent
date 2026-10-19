"""Builds the API responses for tracking from the forecasts. Kept out of the router so it can be tested directly."""
from dataclasses import asdict
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Alert, AuditLog, Course, ForecastSnapshot
from app.services.alert_pipeline import ACTIVE_STATUSES
from app.services.errors import NotFound
from app.services.forecasting import CourseForecast, course_cone
from app.services.risk import RISK_ORDER
from app.services.sessions import fill_band, fill_rate
from app.services.tracking import (
    TrackingFacts, breakdown_from_facts, load_facts, series_from_facts, target_pace, year_fraction,
)
from app.services.tracking_jobs import compute_forecasts
from app.simulator.rules import SIM_END, SIM_START

UPCOMING_LIMIT = 10


def open_alert_ids(db: Session) -> dict[int, int]:
    """{course_id: id of its open alert} in one query."""
    return {cid: aid for aid, cid in db.execute(select(Alert.id, Alert.course_id).where(Alert.status.in_(ACTIVE_STATUSES)))}


def course_row(fc: CourseForecast, alert_id: int | None) -> dict:
    row = asdict(fc)
    for key in ("fleet_show", "fleet_fill", "expected_attempts", "mu", "sigma", "completed_sessions"):
        row.pop(key)
    for key in ("p_show", "p_fill", "p_hit", "seats_needed"):
        row[key] = round(row[key], 3)
    for key in ("projected", "low", "high", "naive_projection", "linear_projection"):
        row[key] = round(row[key], 1)
    row["course_id"] = fc.course_id
    row["open_alert_id"] = alert_id
    return row


def sort_rows(rows: list[dict]) -> list[dict]:
    """High, medium, low, achieved, insufficient data; within a level the least likely to hit target first."""
    return sorted(rows, key=lambda r: (RISK_ORDER[r["risk_level"]], r["p_hit"], r["code"]))


def course_rows(db: Session) -> tuple[TrackingFacts, list[CourseForecast], list[dict]]:
    """Every course's forecast row with a fixed number of queries (no per-course lookups)."""
    facts = load_facts(db)
    forecasts = compute_forecasts(facts)
    alerts = open_alert_ids(db)
    rows = sort_rows([course_row(fc, alerts.get(fc.course_id)) for fc in forecasts])
    return facts, forecasts, rows


def summary(db: Session) -> dict:
    facts, forecasts, _rows = course_rows(db)
    counts = {level: 0 for level in RISK_ORDER}
    for fc in forecasts:
        counts[fc.risk_level] += 1
    mandatory = [fc for fc in forecasts if fc.is_mandatory]
    target_sum = sum(fc.target for fc in mandatory)
    credited = sum(min(fc.attended, fc.target) for fc in mandatory)
    return {
        "current_time": facts.now,
        "percent_year_elapsed": round(100 * year_fraction(facts.now), 1),
        "has_data": any(s.status == "completed" for s in facts.sessions),
        "total_attended": sum(fc.attended for fc in forecasts),
        "total_target": sum(fc.target for fc in forecasts),
        "mandatory_compliance_pct": round(100 * credited / target_sum, 1) if target_sum else 0.0,
        "risk_counts": counts,
        "open_alerts": len(open_alert_ids(db)),
    }


def _series(facts: TrackingFacts, course, fc: CourseForecast, cone: list[dict]) -> list[dict]:
    """Chart points: past = cumulative actual; 'now' joins the forecast; future = forecast cone. Pace on every point."""
    points: dict[datetime, dict] = {SIM_START: {"date": SIM_START, "target_pace": 0.0, "actual": 0}}
    for row in series_from_facts(facts, course):
        points[row["as_of"]] = {"date": row["as_of"], "target_pace": row["target_pace"], "actual": row["cumulative_attended"]}
    now_point = points.get(facts.now)
    if now_point is None:  # cannot happen (the last weekly row ends at now), kept as a guard
        now_point = points[facts.now] = {
            "date": facts.now, "target_pace": round(target_pace(course.target, facts.now), 2), "actual": fc.attended,
        }
    now_point.update(forecast_mean=fc.attended, forecast_low=fc.attended, forecast_high=fc.attended, is_now=True)
    for c in cone:
        points[c["as_of"]] = {
            "date": c["as_of"], "target_pace": round(target_pace(course.target, c["as_of"]), 2),
            **{k: round(c[k], 1) for k in ("forecast_mean", "forecast_low", "forecast_high")},
        }
    return [points[d] for d in sorted(points)]


def _events(db: Session, course_session_ids: set[int], facts: TrackingFacts, code: str) -> list[dict]:
    """Scenario events and cancellations that touched this course, for markers on the chart."""
    events = []
    rows = db.scalars(
        select(AuditLog)
        .where(AuditLog.action.in_(["scenario_event_applied", "session_cancelled"]))
        .order_by(AuditLog.timestamp, AuditLog.id)
    )
    for row in rows:
        if row.action == "scenario_event_applied":
            hit = [sid for sid in row.details.get("sessions_cancelled", []) if sid in course_session_ids]
            if hit:
                events.append({
                    "date": row.timestamp, "kind": row.details.get("kind", "scenario"),
                    "label": f"{row.details.get('description', 'Scenario event')} ({len(hit)} {code} sessions cancelled)",
                })
        elif row.entity_id in course_session_ids:
            released = row.details.get("cancelled_bookings", 0)
            events.append({
                "date": row.timestamp, "kind": "session_cancelled",
                "label": f"Session cancelled by a user ({released} bookings released)",
            })
    return events


def course_detail(db: Session, course_id: int) -> dict:
    if db.get(Course, course_id) is None:
        raise NotFound(f"Course {course_id} not found")
    facts, forecasts, rows = course_rows(db)
    fc = next(f for f in forecasts if f.course_id == course_id)
    course = next(c for c in facts.courses if c.id == course_id)
    row = next(r for r in rows if r["course_id"] == course_id)

    own_sessions = facts.sessions_of(course_id)
    weekly = [
        {"week_start": r["week_start"], "attended": r["attended_in_week"], "no_shows": r["no_shows_in_week"], "closed": r["closed"]}
        for r in series_from_facts(facts, course)
    ]
    snapshots = db.scalars(
        select(ForecastSnapshot).where(ForecastSnapshot.course_id == course_id).order_by(ForecastSnapshot.as_of)
    )
    upcoming = sorted(
        (s for s in own_sessions if s.status == "scheduled" and s.start_time >= facts.now), key=lambda s: s.start_time,
    )[:UPCOMING_LIMIT]
    return {
        "course": row,
        "series": _series(facts, course, fc, course_cone(facts, course, fc)),
        "weekly": weekly,
        "snapshots": [
            {"as_of": s.as_of, "attended": s.attended, "projected": s.projected, "low": s.low, "high": s.high,
             "p_hit": s.p_hit, "naive_projection": s.naive_projection, "risk_level": s.risk_level}
            for s in snapshots
        ],
        "breakdown": {
            "shift": breakdown_from_facts(facts, course_id, "shift"),
            "nationality": breakdown_from_facts(facts, course_id, "nationality"),
        },
        "events": _events(db, {s.id for s in own_sessions}, facts, course.code),
        "upcoming_sessions": [
            {
                "id": s.id, "start_time": s.start_time, "capacity": s.capacity, "booked": s.booked,
                "fill_rate": (None if (r := fill_rate("scheduled", s.capacity, s.booked, 0)) is None else round(r, 3)),
                "fill_band": fill_band("scheduled", fill_rate("scheduled", s.capacity, s.booked, 0)),
            }
            for s in upcoming
        ],
    }


def all_series(db: Session) -> dict[int, list[dict]]:
    """Chart series for every course in one go (for the dashboard's small multiples)."""
    facts, forecasts, _rows = course_rows(db)
    courses = {c.id: c for c in facts.courses}
    return {
        fc.course_id: _series(facts, courses[fc.course_id], fc, course_cone(facts, courses[fc.course_id], fc))
        for fc in forecasts
    }
