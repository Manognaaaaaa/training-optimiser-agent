# Constraint rules (Phase 4)

The checker (`backend/app/services/constraints.py`) takes a proposed plan and returns every rule it breaks.
It is plain deterministic Python: no LLM, no database writes, and it uses the **simulated** clock.

```
POST /api/constraints/check      body = a raw plan; returns { is_valid, violations[], summary{} }
```

## How a plan is checked

1. `parse_plan` validates the shape. Anything malformed becomes one `SCHEMA_INVALID` violation (never an exception).
2. The checker reads what the plan touches into an in-memory snapshot.
3. Actions are applied to the snapshot **in order**, each one checked first. An action that breaks a rule is
   reported and not applied, so one bad step does not distort later ones. A bad `add_session` is still
   registered, so later steps using its temp ref are not also reported as unknown.
4. All violations are collected. Each has `code`, `category`, `action_index` (0 = first action, `null` = whole plan),
   a plain-English `message`, and the `entity_type` / `entity_id` involved.

Plan actions (`type` field): `move_driver`, `enroll_driver`, `add_session`.
`"new:1"`, `"new:2"` are temp refs for sessions created earlier in the same plan. They are accepted for `session_id`
(enroll) and `to_session_id` (move).

## Violation codes

Constants live at the top of `constraints.py` unless another file is named.

| Code | Category | What it means | Controlled by |
|---|---|---|---|
| `SCHEMA_INVALID` | schema | The plan is not in the required format (missing or unknown keys, text where a number belongs, bad temp ref, timezone-aware times, 0 or more than 50 actions). | `MAX_ACTIONS` in `schemas/plan.py` |
| `UNKNOWN_DRIVER` | hallucination | The driver id does not exist. | none |
| `UNKNOWN_SESSION` | hallucination | The session id does not exist. | none |
| `UNKNOWN_TRAINER` | hallucination | The trainer id does not exist. | none |
| `UNKNOWN_COURSE` | hallucination | The course id (plan's or a new session's) does not exist. | none |
| `UNKNOWN_TEMP_REF` | hallucination | An action uses `new:N` but no *earlier* `add_session` created it. | none |
| `NOT_ENROLLED` | hallucination | `move_driver` takes a driver out of a session they are not booked into. | none |
| `INACTIVE_DRIVER` | validity | `drivers.is_active` is false. | none |
| `SESSION_NOT_SCHEDULED` | validity | The target session is completed or cancelled. | none |
| `SESSION_IN_PAST` | validity | The target or new session starts at or before the simulated "now". | `sim_state.current_time` |
| `WRONG_COURSE` | validity | The target session (or new session) is not for the plan's course. Moves stay inside one course. | none |
| `ALREADY_COMPLETED` | validity | The driver already attended this course in the target year. | `SIM_YEAR` in `simulator/rules.py` |
| `ALREADY_BOOKED` | validity | The driver already has a booked seat for this course (in the target session or another). A move does not count the seat it leaves. | none |
| `BAD_SESSION_TIMES` | validity | New session: ends at or before it starts; length differs from the course's session length; or falls outside the target year. | `MAX_SESSION_HOURS`, `SIM_YEAR` in `simulator/rules.py` |
| `BAD_CAPACITY` | validity | New session capacity is 0 or less, or above `courses.default_capacity`. | `courses.default_capacity` |
| `DUPLICATE_TEMP_REF` | validity | Two `add_session` actions use the same `new:N`. | none |
| `OVER_CAPACITY` | capacity | Booked drivers would exceed the session's capacity after the action. | `training_sessions.capacity` |
| `DRIVER_UNAVAILABLE` | availability | The session overlaps a `driver_unavailability` window. | none |
| `DRIVER_ON_SHIFT` | availability | The session overlaps the driver's shift, or starts too soon after it ends. Reuses the simulator's roster rule. | `SHIFT_HOURS`, `REST_HOURS`, `ROSTER_REST_STARTS` in `simulator/rules.py` |
| `DRIVER_DOUBLE_BOOKED` | double_booking | The driver is in another overlapping session (any course, including one added earlier in the plan). | none |
| `TRAINER_DOUBLE_BOOKED` | double_booking | A new session overlaps another scheduled session of the same trainer. | none |
| `TRAINER_WEEKLY_LOAD` | trainer_load | The trainer would have more sessions that week than `trainers.max_sessions_per_week`. | `trainers.max_sessions_per_week` |
| `MAX_SESSIONS_PER_DAY` | rest | The driver would start more sessions on one calendar day than allowed. | `MAX_DRIVER_SESSIONS_PER_DAY` (default 1) |
| `MIN_REST_GAP` | rest | Less than the minimum number of hours between the end of one of the driver's sessions and the start of the next. | `MIN_REST_HOURS_BETWEEN_SESSIONS` (default 12) |
| `NIGHT_SHIFT_MORNING` | rest | The driver worked a night shift that ended this morning and the session starts before the cut-off. | `NIGHT_SHIFT_EARLIEST_START` (default 12:00) |

The `hallucination` category is what Phase 8 uses for the catch-rate metric.

## Decisions where the data model or earlier phases shaped the rule

- **Session length.** `courses.duration_hours` is the whole course, but long courses are taught in sessions of at
  most `MAX_SESSION_HOURS` (4h). `BAD_SESSION_TIMES` compares against `session_length_hours(duration_hours)`, the same
  function the seed uses, so every seeded session is acceptable.
- **Weeks.** Trainer weekly load uses Sunday–Saturday weeks (`week_start()`), the same as the Trainers page and the
  sessions service, not ISO (Monday-start) weeks.
- **Target year.** A plan carries no year, so `SIM_YEAR` (2026) is used for `ALREADY_COMPLETED` and the year bounds.
- **`DRIVER_ON_SHIFT` is an addition.** It was not in the original list. Without it the agent could propose bookings
  that the simulator treats as impossible (a driver in the middle of their shift).
- **`NIGHT_SHIFT_MORNING` and rotating drivers.** It applies when `effective_shift(driver, previous day)` is night:
  always for night drivers, and in their night weeks for rotating drivers. It overlaps with the 8h rest in
  `DRIVER_ON_SHIFT` on working days, but still catches a night driver's rest days.
- **"In the past" means at or before now**, matching the ERP bookings rule ("session has already started").
- **Counting.** Only `scheduled` sessions and `booked` enrollments count for double booking, trainer load and capacity.
  Cancelled sessions and cancelled enrollments are ignored.
- **A move from a session to itself** is not flagged on its own; it is only rejected if it breaks another rule.
- **Unknown ids skip the other rules for that action.** There is nothing real to check them against.
