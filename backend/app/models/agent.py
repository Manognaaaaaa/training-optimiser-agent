from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime)  # simulated time, not real time
    risk_level: Mapped[str] = mapped_column(String(20))  # low / medium / high
    projected_completions: Mapped[int] = mapped_column(Integer)
    target_completions: Mapped[int] = mapped_column(Integer)
    message: Mapped[str | None] = mapped_column(Text, default=None)  # LLM-written text
    # open -> generating -> plans_ready | no_valid_plans -> approved | rejected | dismissed; also resolved / expired / superseded
    status: Mapped[str] = mapped_column(String(20), default="open")
    # Phase 3: why the course is at risk. details = {p_hit, low, high, reasons: [...], last_change, ...}
    shortfall_type: Mapped[str | None] = mapped_column(String(20), default=None)  # capacity_gap / attendance_gap / pool_gap
    details: Mapped[Any] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)  # simulated time


class ForecastSnapshot(Base):
    """The forecast for one course as it stood at the end of a simulated week (history for charts and the backtest)."""

    __tablename__ = "forecast_snapshots"
    __table_args__ = (UniqueConstraint("course_id", "as_of"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"))
    as_of: Mapped[datetime] = mapped_column(DateTime)  # simulated time
    attended: Mapped[int] = mapped_column(Integer)
    projected: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    p_hit: Mapped[float] = mapped_column(Float)
    naive_projection: Mapped[float] = mapped_column(Float)
    linear_projection: Mapped[float] = mapped_column(Float)
    risk_level: Mapped[str] = mapped_column(String(20))
    shortfall_type: Mapped[str] = mapped_column(String(20))


class PlannerRun(Base):
    """One click of "Generate plans": N samples for one alert. Times are simulated, except duration_ms (real wall time)."""

    __tablename__ = "planner_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    alert_id: Mapped[int] = mapped_column(ForeignKey("alerts.id"))
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"))
    status: Mapped[str] = mapped_column(String(20), default="queued")  # queued / running / done / failed
    n_requested: Mapped[int] = mapped_column(Integer)
    n_valid: Mapped[int] = mapped_column(Integer, default=0)
    n_invalid: Mapped[int] = mapped_column(Integer, default=0)
    n_schema_invalid: Mapped[int] = mapped_column(Integer, default=0)
    n_no_plan: Mapped[int] = mapped_column(Integer, default=0)
    n_step_limit: Mapped[int] = mapped_column(Integer, default=0)
    n_duplicates: Mapped[int] = mapped_column(Integer, default=0)
    n_llm_errors: Mapped[int] = mapped_column(Integer, default=0)
    model: Mapped[str] = mapped_column(String(100), default="")
    prompt_version: Mapped[str] = mapped_column(String(20), default="")
    started_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, default=None)


class Plan(Base):
    """One candidate plan from the agent (one sample of a planner run), valid or not."""

    __tablename__ = "plans"

    id: Mapped[int] = mapped_column(primary_key=True)
    alert_id: Mapped[int] = mapped_column(ForeignKey("alerts.id"))
    actions: Mapped[Any] = mapped_column(JSON)  # list of structured actions proposed by the agent
    is_valid: Mapped[bool] = mapped_column(Boolean, default=False)
    rejection_reasons: Mapped[Any] = mapped_column(JSON, default=list)  # final checker violations, as dicts
    score: Mapped[float | None] = mapped_column(Float, default=None)  # same number as score_total
    rank: Mapped[int | None] = mapped_column(Integer, default=None)  # 1 = best; None for plans that were not ranked
    # invalid / valid / approved / rejected / superseded / stale / expired
    status: Mapped[str] = mapped_column(String(20), default="invalid")
    # Phase 6: ranking
    score_total: Mapped[float | None] = mapped_column(Float, default=None)
    score_breakdown: Mapped[Any] = mapped_column(JSON, default=dict)
    scoring_version: Mapped[str | None] = mapped_column(String(20), default=None)
    # Phase 5
    run_id: Mapped[int | None] = mapped_column(ForeignKey("planner_runs.id"), default=None)
    sample_index: Mapped[int | None] = mapped_column(Integer, default=None)
    temperature: Mapped[float | None] = mapped_column(Float, default=None)
    strategy: Mapped[str | None] = mapped_column(String(200), default=None)
    outcome: Mapped[str | None] = mapped_column(String(20), default=None)  # valid / invalid / schema_invalid / no_plan / step_limit
    rationale: Mapped[str | None] = mapped_column(Text, default=None)
    plan_hash: Mapped[str | None] = mapped_column(String(64), default=None)
    is_duplicate: Mapped[bool] = mapped_column(Boolean, default=False)
    draft_violations: Mapped[Any] = mapped_column(JSON, default=list)  # results of the agent's own validate_plan calls
    fabricated_ids: Mapped[Any] = mapped_column(JSON, default=list)  # [{"type": "driver", "id": 99999}, ...]
    checker_caught: Mapped[bool | None] = mapped_column(Boolean, default=None)
    trace: Mapped[Any] = mapped_column(JSON, default=list)
    n_steps: Mapped[int] = mapped_column(Integer, default=0)
    n_validate_calls: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)  # simulated time


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime)  # simulated time
    actor: Mapped[str] = mapped_column(String(20))  # agent / user / system
    action: Mapped[str] = mapped_column(String(100))
    entity_type: Mapped[str] = mapped_column(String(50))
    entity_id: Mapped[int | None] = mapped_column(Integer, default=None)
    details: Mapped[Any] = mapped_column(JSON, default=dict)


class SimState(Base):
    """Single row (id=1) holding the current simulated time."""

    __tablename__ = "sim_state"

    id: Mapped[int] = mapped_column(primary_key=True)
    current_time: Mapped[datetime] = mapped_column(DateTime)


class AlertMessage(Base):
    """One attempt by the LLM (or the fallback template) to write the trainer-facing text for an alert."""

    __tablename__ = "alert_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    alert_id: Mapped[int] = mapped_column(ForeignKey("alerts.id"))
    planner_run_id: Mapped[int | None] = mapped_column(ForeignKey("planner_runs.id"), default=None)
    model: Mapped[str] = mapped_column(String(100), default="")
    prompt_version: Mapped[str] = mapped_column(String(40), default="")
    raw_output: Mapped[str] = mapped_column(Text, default="")
    parsed: Mapped[Any] = mapped_column(JSON, default=dict)  # the message that was checked (empty if it could not be parsed)
    grounding_ok: Mapped[bool] = mapped_column(Boolean, default=False)
    grounding_issues: Mapped[Any] = mapped_column(JSON, default=list)  # [{field, token, reason}]
    used_fallback: Mapped[bool] = mapped_column(Boolean, default=False)
    attempts: Mapped[int] = mapped_column(Integer, default=1)  # this row's attempt number (1, 2, then the fallback row)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)  # simulated time


class AlertDecision(Base):
    """What a trainer decided about an alert or one of its plans."""

    __tablename__ = "alert_decisions"

    id: Mapped[int] = mapped_column(primary_key=True)
    alert_id: Mapped[int] = mapped_column(ForeignKey("alerts.id"))
    plan_id: Mapped[int | None] = mapped_column(ForeignKey("plans.id"), default=None)
    decision: Mapped[str] = mapped_column(String(20))  # approve / reject_plan / reject_alert / dismiss
    actor_trainer_id: Mapped[int | None] = mapped_column(ForeignKey("trainers.id"), default=None)
    reason_code: Mapped[str | None] = mapped_column(String(40), default=None)
    reason_text: Mapped[str | None] = mapped_column(Text, default=None)
    sim_time: Mapped[datetime] = mapped_column(DateTime)
    real_time: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))


class CalendarOutbox(Base):
    """Changes an approved plan made, waiting for the Phase 7 Google Calendar sync."""

    __tablename__ = "calendar_outbox"

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id"))
    change_type: Mapped[str] = mapped_column(String(20))  # create_session / enroll_driver / move_driver
    payload: Mapped[Any] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)  # simulated time
