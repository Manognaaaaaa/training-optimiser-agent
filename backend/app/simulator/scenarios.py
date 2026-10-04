"""Built-in problems. If nobody intervenes, these make some courses miss their target.

Each scenario is documented here so it can be explained in the report:

1. NIGHT-SHIFT MISMATCH  Defensive Driving (DEF) is mostly scheduled at 09:00. Night-shift
   drivers finish work at 06:00, so the 8h rest rule blocks most 09:00 bookings for them, and
   the few who are booked often no-show (attendance penalty). The course falls behind and the
   gap is concentrated on night shift.
2. TRAINER LEAVE         On 2026-04-19 trainer id 1 starts 6 weeks of unplanned leave. When the
   clock reaches that date the simulator cancels their sessions in the window. That trainer
   is the main trainer for First Aid (FA) and Hazmat (HAZ), so those courses lose capacity.
3. SEASONAL DIPS         Attendance is lower during Ramadan and the summer peak (constants below,
   used by the attendance model in engine.py).
4. HEALTHY CONTROL       Road Safety Refresher (RSR) has a lower target (74-78% of drivers), 145% planned seats, afternoon
   sessions and high demand, so it should finish clearly (14-29%) above target.
"""
from dataclasses import dataclass, field
from datetime import date
from typing import Any

# --- Scenario 1: night-shift mismatch ---------------------------------------------------
NIGHT_MISMATCH_COURSE = "DEF"
# Share of DEF sessions at each start hour (default for other courses is equal thirds).
SLOT_WEIGHTS_BY_COURSE: dict[str, dict[int, float]] = {
    "DEF": {9: 0.85, 14: 0.10, 18: 0.05},
    "RSR": {14: 0.5, 18: 0.5},  # control course: afternoon only, works for every shift
}

# --- Scenario 3: seasonal dips (subtracted from the attendance probability) -------------
RAMADAN_ATTENDANCE_PENALTY = 0.10
SUMMER_ATTENDANCE_PENALTY = 0.08

# --- Scenario 4: healthy control --------------------------------------------------------
CONTROL_COURSE = "RSR"

# How full the booking process tries to make sessions: each session aims for a random share
# of its capacity between (demand - 0.15) and demand. Below 1 means seats are left empty.
DEFAULT_DEMAND = 0.95
DEMAND_BY_COURSE: dict[str, float] = {
    CONTROL_COURSE: 1.05,  # popular course: seats fill almost completely
}


@dataclass(frozen=True)
class ScenarioEvent:
    """A dated event that the simulator applies exactly once, when the clock reaches ``date``."""

    key: str
    date: date
    kind: str
    description: str
    params: dict[str, Any] = field(default_factory=dict)


# Scenario 2 lives here. Add more events to the list; the engine handles them by ``kind``.
SCENARIO_EVENTS: list[ScenarioEvent] = [
    ScenarioEvent(
        key="trainer_leave_q2",
        date=date(2026, 4, 19),
        kind="trainer_leave",
        description="Trainer 1 goes on 6 weeks of unplanned leave; their sessions in the window are cancelled.",
        params={"trainer_id": 1, "weeks": 6},
    ),
]

# Trainer that specialises in the courses hit by the trainer-leave scenario (seed uses this).
LEAVE_TRAINER_ID = 1
