"""Importing every model here makes sure Base.metadata knows all 16 tables."""
from app.models.agent import (
    Alert, AlertDecision, AlertMessage, AuditLog, CalendarOutbox, ForecastSnapshot, Plan, PlannerRun, SimState,
)
from app.models.master import Course, Driver, Trainer, TrainingTarget
from app.models.scheduling import DriverUnavailability, Enrollment, TrainingSession

__all__ = [
    "Alert",
    "AlertDecision",
    "AlertMessage",
    "CalendarOutbox",
    "AuditLog",
    "Course",
    "Driver",
    "DriverUnavailability",
    "Enrollment",
    "ForecastSnapshot",
    "Plan",
    "PlannerRun",
    "SimState",
    "Trainer",
    "TrainingSession",
    "TrainingTarget",
]
