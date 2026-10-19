# Ranking, alert messages and decisions (Phase 6)

After a planner run finishes, three things happen in code, in this order. The LLM only writes words in step 2.

1. **Rank**: every valid plan gets a deterministic score (`services/ranking.py`).
2. **Write**: the LLM writes the trainer alert, and a deterministic check verifies it (`services/alert_writer.py`).
3. **Decide**: a trainer approves or rejects (`services/decisions.py`). Approving re-checks and applies the plan.

`services/alert_pipeline.py` glues 1 and 2 to the end of `execute_run`, so a finished run leads straight to ranked plans,
a message, and `alerts.status = plans_ready` (or `no_valid_plans`).

## Scoring (version `v1`)

Only plans whose final checker verdict is clean are scored. Invalid plans stay stored with their violations and are shown separately.
Each component is 0 to 1 (higher is better); the weights live in `config.py` and must sum to 1 (the app refuses to start otherwise).

| Component | Weight | Definition | Why |
|---|---|---|---|
| `closure` | 0.35 | `min(1, expected_gain / shortfall_gap)` | The main goal is to close the gap. |
| `confidence` | 0.20 | P(hit target) after the plan | Closing on average is not enough; we want to be likely to hit it. |
| `fairness` | 0.20 | `1 - max_excess` (below) | The same drivers must not carry every reallocation. |
| `disruption` | 0.15 | `1 - min(1, (drivers_moved + 2 * sessions_added) / 20)` | Fewer changes are easier to carry out. A new session counts double. |
| `resource` | 0.10 | `1 - min(1, trainer_hours_added / 16)` | Trainer time is limited. |

`score_total` is the weighted sum rounded to 4 decimals. Ties are broken by higher `closure`, then fewer `drivers_moved`, then plan hash, so the order is fully deterministic.
The weights used are stored inside `score_breakdown`, with `scoring_version = "v1"`.

### What-if forecast (closure and confidence)

The Phase 3 forecast expects `mu = sum(n * q)` completions from the remaining sessions with variance `sum(n * q * (1 - q))`.
Applying a plan adds seats (enrol, move target) and removes seats (move source). Each seat is worth that driver's own chance of showing up
(their record on this course, shrunk toward the course rate; the course rate when there is no record). So:

- `mu_after = mu_before + sum(q added) - sum(q removed)`, and the variance changes the same way;
- `expected_gain = mu_after - mu_before`;
- `shortfall_gap = max(0, target - attended - mu_before)`;
- `p_hit_before` / `p_hit_after` come from `forecasting.project_moments` (the same formula as the dashboard).

Consequences worth knowing: a **move on its own gains nothing** (the same driver leaves one seat and takes another); the gain comes from
**new seats**. A new empty session gains nothing until the plan books drivers into it. This is conservative on purpose.

### Fairness

Burden = the distinct drivers a plan moves or newly books. For `shift` and for `nationality`, for each group:
`excess = burden_share - pool_share` (the pool is the drivers who could still complete the course). `max_excess` is the largest positive excess
over both groupings, and `fairness = 1 - max_excess`. If one group takes more than 60% of the burden while being under 60% of the pool,
`fairness_flag = true` (red chip in the UI). Per-group shares are stored in `score_breakdown.fairness_groups`.

The top `TOP_K` (default 3) plans are presented; the rest stay ranked but collapsed.

## The alert message

The LLM sees only a **fact pack** built in code: course, target, actual, forecast with 90% range, chance of hitting, risk, shortfall type and reasons, and for each
presented plan its id, rank, score, components, before/after chance and a change list with real driver names, ids, session ids and dates.
It must return this JSON (validated with Pydantic, `extra = forbid`):

```json
{
  "headline": "max 90 characters",
  "summary": "2 to 3 sentences on the risk",
  "why_at_risk": ["short bullet"],
  "plans": [{"plan_id": 0, "rationale": "1 to 2 sentences", "tradeoffs": "1 sentence"}],
  "recommended_plan_id": 0,
  "caveats": ["optional"]
}
```

### Grounding check (deterministic)

- `recommended_plan_id` must be the rank 1 plan; every `plan_id` must be a presented plan, and each presented plan must be described once.
- Every number in every text field must match a number in the fact pack (whole-number or one-decimal rounding allowed; a fraction may be quoted as a percentage; "90%" is allowed as the range label; small counts up to the number of plans are allowed).
- A real driver or trainer name that is not in the fact pack is a failure. A capitalised multi-word name that is not in the fact pack vocabulary is a failure too (a heuristic: it can miss a single-word name).
- Each problem is stored as `{field, token, reason}`.

### Retry and fallback

Temperature 0.2. On a parse or grounding failure the LLM gets one retry with the problems listed. If that fails too, a template built only from fact pack values is used
(`used_fallback = true`; the template is itself grounding-checked). **Every attempt is a row in `alert_messages`** (`attempts` = the attempt number; the fallback row carries the number of LLM attempts made),
and one audit row records model, prompt version, prompt hash, grounding results and whether the fallback was used. The prompt is `llm/prompt_files/alert_writer_v1.txt`.

## State machines

```
alert:  open --generate--> generating --run ok--> plans_ready --approve--> approved
                               |                      |  \--reject all / last recommended rejected--> rejected
                               |                      \--dismiss--> dismissed
                               \--run ok, no valid plan--> no_valid_plans --generate--> generating
        any active alert --course recovers--> resolved        (weekly sync)
        rejected / dismissed alerts reopen as a new alert only if the risk level gets worse
        plans_ready --regenerate--> generating (old plans become superseded when the new run finishes)

plan:   invalid                                  (never ranked)
        valid --approve--> approved              (the others of that alert become superseded)
        valid --reject--> rejected
        valid --re-check fails on approve--> stale
        valid --first session already started--> expired
        valid --newer run / alert closed--> superseded
```

`active` alerts (open, generating, plans_ready, no_valid_plans) are the "one open alert per course": the weekly sync refreshes their numbers in place.

## Approving

`POST /api/plans/{id}/approve` (body `{actor_trainer_id}`):

1. Plan must be `valid` and its alert `plans_ready`, else 409.
2. If the simulated clock has reached the earliest session in the plan, the plan becomes `expired` (409).
3. The checker runs again on today's data. Any violation makes the plan `stale` and returns 409 with the violations (`reason: "stale"`).
4. One transaction applies every action in order (new sessions first get real ids; moves cancel the old booking) and creates a `calendar_outbox` row per change (`create_session`, `enroll_driver`, `move_driver`). Any error rolls everything back.
5. Plan `approved`, other plans `superseded`, alert `approved`, an `alert_decisions` row and audit rows are written.
6. Approving the same plan again returns the first result (`already_approved: true`) and changes nothing.

Known gap (from Phase 5, unchanged): the checker does not enforce the Sun-Thu 09/14/18 slot rule for `add_session`, so an approved new session is not guaranteed to be on a regular slot.

## API

| Method and path | What it does |
|---|---|
| `GET /api/alerts?status=&risk_level=&course_id=&since=` | Alerts, high risk first then newest. `status` may be a comma list. |
| `GET /api/alerts/counts` | Count per status (sidebar badge uses `plans_ready`). |
| `GET /api/alerts/{id}` | Alert, forecast, latest message with grounding status, presented plans (score breakdown, change list), other valid plans, checker-rejected plans, latest run, decisions. |
| `POST /api/alerts/{id}/generate` `{n}` | Start the planner (background run; poll the alert). Allowed from `open`, `no_valid_plans`, `plans_ready`. |
| `POST /api/alerts/{id}/regenerate` `{n}` | Same; the old plans become `superseded` when the new run finishes. |
| `POST /api/alerts/{id}/reject` | Reject the whole alert. Body: `actor_trainer_id`, `reason_code`, `reason_text`. |
| `POST /api/alerts/{id}/dismiss` | Dismiss. Body: `actor_trainer_id`, optional `reason_text`. |
| `POST /api/plans/{id}/approve` | Approve (see above). |
| `POST /api/plans/{id}/reject` | Reject one plan. `reason_code` is one of `not_feasible`, `unfair`, `too_disruptive`, `trainer_unavailable`, `other` (`other` needs text). When no recommended plan is left the alert becomes `rejected`. |

The earlier planner endpoints (`/api/planner/*`) still work; `POST /api/planner/runs` now also accepts alerts in `plans_ready` and `no_valid_plans`.

## Config

`SCORE_WEIGHT_*` (sum to 1), `DISRUPTION_CAP` (20), `TRAINER_HOURS_CAP` (16), `TOP_K` (3), `FAIRNESS_FLAG_BURDEN` / `FAIRNESS_FLAG_POOL` (0.6),
`ALERT_WRITER_TEMPERATURE` (0.2), `AUTO_PLAN_ON_HIGH_RISK` (false: when true, advancing the clock queues plan generation for one new high-risk alert that has never been planned).
