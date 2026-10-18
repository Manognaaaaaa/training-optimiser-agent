"""Importing every model here makes sure Base.metadata knows all 13 tables."""
from app.models.agent import Alert, AuditLog, ForecastSnapshot, Plan, PlannerRun, SimState
from app.models.master import Course, Driver, Trainer, TrainingTarget
from app.models.scheduling import DriverUnavailability, Enrollment, TrainingSession

__all__ = [
    "Alert",
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
