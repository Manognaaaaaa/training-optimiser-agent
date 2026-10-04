"""The plan format the agent will output (Phase 5) and the result the constraint checker returns.

Everything here is strict on purpose: an LLM can send anything, so unknown keys, strings where
numbers belong and timezone-aware times are all rejected instead of being quietly "fixed".
"""
from collections import Counter
from datetime import datetime
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError, field_validator

MAX_ACTIONS = 50  # a plan with more actions than this is rejected as malformed

# A reference to a session that an earlier add_session action in the same plan creates: "new:1", "new:2", ...
TempRef = Annotated[str, Field(pattern=r"^new:\d+$")]
# Either the id of an existing session or a temp ref.
SessionRef = Union[StrictInt, TempRef]


class _Strict(BaseModel):
    """Base class: unknown keys are errors."""

    model_config = ConfigDict(extra="forbid")


# --- Actions ---------------------------------------------------------------------------------
class MoveDriver(_Strict):
    """Take a driver out of one session and book them into another one."""

    type: Literal["move_driver"]
    driver_id: StrictInt
    from_session_id: StrictInt
    to_session_id: SessionRef


class EnrollDriver(_Strict):
    """Book a driver into a session (they are not moved from anywhere)."""

    type: Literal["enroll_driver"]
    driver_id: StrictInt
    session_id: SessionRef


class AddSession(_Strict):
    """Create a new session. Later actions in the plan can refer to it by ``temp_ref``."""

    type: Literal["add_session"]
    temp_ref: TempRef
    course_id: StrictInt
    trainer_id: StrictInt
    start: datetime
    end: datetime
    location: str = Field(min_length=1, max_length=100)
    capacity: StrictInt

    @field_validator("start", "end")
    @classmethod
    def _no_timezone(cls, value: datetime) -> datetime:
        """The database stores plain local times, so a timezone offset would make comparisons wrong."""
        if value.tzinfo is not None:
            raise ValueError("use local clock time without a timezone offset")
        return value


Action = Annotated[Union[MoveDriver, EnrollDriver, AddSession], Field(discriminator="type")]


class Plan(_Strict):
    """One candidate redistribution plan for an at-risk course."""

    course_id: StrictInt
    alert_id: StrictInt | None = None
    actions: list[Action] = Field(min_length=1, max_length=MAX_ACTIONS)
    rationale: str | None = Field(default=None, max_length=5000)


# --- Checker output --------------------------------------------------------------------------
class Violation(BaseModel):
    """One broken rule."""

    code: str  # stable machine name, e.g. OVER_CAPACITY
    category: str  # hallucination / validity / capacity / availability / double_booking / trainer_load / rest / schema
    action_index: int | None = None  # position in plan.actions (0 = first); None for whole-plan problems
    message: str  # plain English
    entity_type: str | None = None  # driver / session / trainer / course / plan
    entity_id: int | str | None = None  # a temp ref such as "new:1" is a string


class CheckResult(BaseModel):
    is_valid: bool
    violations: list[Violation]
    summary: dict[str, int]  # violations per category (only categories that occurred)

    @classmethod
    def from_violations(cls, violations: list[Violation]) -> "CheckResult":
        counts = Counter(v.category for v in violations)
        return cls(is_valid=not violations, violations=violations, summary=dict(counts))


# --- Parsing ---------------------------------------------------------------------------------
def _describe(error: ValidationError) -> str:
    """Turn pydantic's error list into one short sentence a person can read."""
    parts = []
    for item in error.errors()[:5]:
        where = ".".join(str(p) for p in item["loc"]) or "plan"
        parts.append(f"{where}: {item['msg']}")
    more = error.error_count() - len(parts)
    suffix = f" (and {more} more)" if more > 0 else ""
    return "The plan does not match the required format. " + "; ".join(parts) + suffix


def parse_plan(raw: Any) -> Plan | Violation:
    """Validate raw (LLM) output. Returns a Plan, or a SCHEMA_INVALID violation. Never raises."""
    try:
        return Plan.model_validate(raw)
    except ValidationError as err:
        message = _describe(err)
    except (ValueError, TypeError, RecursionError) as err:
        message = f"The plan could not be read: {err}"
    return Violation(code="SCHEMA_INVALID", category="schema", message=message, entity_type="plan")
