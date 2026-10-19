"""Glue between the simulator and tracking: compute every forecast, store a weekly snapshot, sync alerts.

The simulator engine only calls ``weekly_close``; it knows nothing about forecasting.
"""
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.live.bus import publish_after_commit, publish_alert
from app.models import Alert, ForecastSnapshot
from app.services.alerts import AlertChanges, sync_alerts
from app.services.forecasting import CourseForecast, fleet_rates, forecast_course
from app.services.risk import assess
from app.services.tracking import TrackingFacts, load_facts


def compute_forecasts(facts: TrackingFacts, inflation: float | None = None) -> list[CourseForecast]:
    """Forecast + risk for every course, from facts already loaded (no queries)."""
    fleet = fleet_rates(facts)
    return [assess(facts, forecast_course(facts, course, fleet, inflation)) for course in facts.courses]


def write_snapshots(db: Session, forecasts: list[CourseForecast], as_of: datetime) -> int:
    """One snapshot per course at ``as_of``. Re-running the same moment updates the rows instead of duplicating them."""
    existing = {s.course_id: s for s in db.scalars(select(ForecastSnapshot).where(ForecastSnapshot.as_of == as_of))}
    for fc in forecasts:
        values = dict(
            attended=fc.attended, projected=round(fc.projected, 2), low=round(fc.low, 2), high=round(fc.high, 2),
            p_hit=round(fc.p_hit, 4), naive_projection=round(fc.naive_projection, 2),
            linear_projection=round(fc.linear_projection, 2), risk_level=fc.risk_level, shortfall_type=fc.shortfall_type,
        )
        row = existing.get(fc.course_id)
        if row is None:
            db.add(ForecastSnapshot(course_id=fc.course_id, as_of=as_of, **values))
        else:
            for key, value in values.items():
                setattr(row, key, value)
    return len(forecasts)


@dataclass
class WeeklyCloseResult:
    as_of: datetime
    snapshots: int
    changes: AlertChanges = field(default_factory=AlertChanges)


def weekly_close(db: Session) -> WeeklyCloseResult:
    """Snapshot every course's forecast at the current sim time and sync the alerts. The caller commits.

    The engine calls this right after a Saturday has been processed (the week just closed).
    Safe to run again for the same moment: snapshots are updated in place and alerts only change when risk changes.
    """
    db.flush()  # make the day's enrollment changes visible to the queries below
    facts = load_facts(db)
    forecasts = compute_forecasts(facts)
    snapshots = write_snapshots(db, forecasts, facts.now)
    changes = sync_alerts(db, forecasts, facts.now)
    db.flush()
    publish_after_commit(db, "forecast.updated", {"course_ids": sorted(fc.course_id for fc in forecasts)})
    for alert_id in changes.opened:
        publish_alert(db, db.get(Alert, alert_id), "alert.created")
    for alert_id in [*changes.escalated, *changes.deescalated, *changes.resolved]:
        publish_alert(db, db.get(Alert, alert_id), "alert.updated")
    return WeeklyCloseResult(facts.now, snapshots, changes)
