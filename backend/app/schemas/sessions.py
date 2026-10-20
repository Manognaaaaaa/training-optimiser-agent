from datetime import datetime

from pydantic import BaseModel, Field, field_validator


def _naive(value: datetime | None) -> datetime | None:
    """Datetimes are naive Dubai local time; reject anything with a timezone so nothing shifts silently."""
    if value is not None and value.tzinfo is not None:
        raise ValueError("send a plain local time without a timezone, e.g. 2026-03-01T09:00:00")
    return value


class SessionCreate(BaseModel):
    course_id: int
    trainer_id: int
    start_time: datetime
    end_time: datetime | None = None  # computed from the course duration if empty
    location: str = Field("Main Training Room", min_length=1, max_length=100)
    capacity: int | None = None  # defaults to the course's default_capacity

    _check_tz = field_validator("start_time", "end_time")(_naive)


class SessionUpdate(BaseModel):
    trainer_id: int | None = None
    start_time: datetime | None = None
    end_time: datetime | None = None
    location: str | None = Field(None, min_length=1, max_length=100)
    capacity: int | None = None

    _check_tz = field_validator("start_time", "end_time")(_naive)


class SessionOut(BaseModel):
    id: int
    course_id: int
    course_code: str
    course_name: str
    trainer_id: int
    trainer_name: str
    start_time: datetime
    end_time: datetime
    location: str
    capacity: int
    status: str
    source: str
    booked: int
    attended: int
    no_show: int
    fill_rate: float | None
    fill_band: str
    is_editable: bool  # scheduled and not started yet


class EnrollmentCreate(BaseModel):
    driver_id: int


class EnrollmentOut(BaseModel):
    id: int
    session_id: int
    driver_id: int
    employee_code: str
    name: str
    shift: str
    nationality: str
    status: str


class EligibleDriver(BaseModel):
    id: int
    employee_code: str
    name: str
    shift: str
    nationality: str
    depot: str


class CalendarEvent(BaseModel):
    id: int
    course_id: int
    course_code: str
    course_name: str
    trainer_id: int
    trainer_name: str
    start: datetime
    end: datetime
    location: str
    capacity: int
    status: str
    source: str
    booked: int
    attended: int
    fill_rate: float | None
    fill_band: str


class CalendarResponse(BaseModel):
    sim_now: datetime
    events: list[CalendarEvent]
