"""A trainer's decisions: approve a plan, reject a plan, reject or dismiss an alert.

The only code that changes enrollments and sessions because of an agent plan. Approving:
1. is only possible for a ``valid`` plan whose alert is ``plans_ready``;
2. refuses plans whose first session has already started (plan -> ``expired``);
3. re-runs the constraint checker against the database as it is now (plan -> ``stale`` if anything broke);
4. applies every action in ONE transaction and queues a ``calendar_outbox`` row per change;
5. is idempotent: approving an already approved plan returns the first result and changes nothing.
The LLM is never involved in any of this.
"""
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Alert, AlertDecision, CalendarOutbox, Enrollment, Plan, Trainer, TrainingSession
from app.schemas.plan import AddSession, EnrollDriver, MoveDriver, Violation, parse_plan
from app.services.alert_pipeline import ACTIVE_STATUSES, set_alert_status
from app.services.audit import log_event
from app.services.constraints import check_plan
from app.services.errors import Conflict, Invalid, NotFound
from app.services.sim_clock import get_sim_now

REJECT_REASONS = ("not_feasible", "unfair", "too_disruptive", "trainer_unavailable", "other")
REJECTABLE_ALERT_STATUSES = ("open", "plans_ready", "no_valid_plans")


# --- Small helpers -------------------------------------------------------------------------------------
def _get_plan(db: Session, plan_id: int) -> Plan:
    plan = db.get(Plan, plan_id)
    if plan is None:
        raise NotFound(f"Plan {plan_id} not found")
    return plan


def _get_alert(db: Session, alert_id: int) -> Alert:
    alert = db.get(Alert, alert_id)
    if alert is None:
        raise NotFound(f"Alert {alert_id} not found")
    return alert


def _check_actor(db: Session, trainer_id: int | None) -> int | None:
    if trainer_id is not None and db.get(Trainer, trainer_id) is None:
        raise NotFound(f"Trainer {trainer_id} not found")
    return trainer_id


def _check_reason(code: str | None, text: str | None, required: bool) -> tuple[str | None, str | None]:
    if code is None:
        if required:
            raise Invalid("A reason is required", {"reason_code": "required"})
        return None, (text or None)
    if code not in REJECT_REASONS:
        raise Invalid(f"reason_code must be one of: {', '.join(REJECT_REASONS)}", {"reason_code": "not a known reason"})
    if code == "other" and not (text or "").strip():
        raise Invalid("Please explain the reason", {"reason_text": "required when the reason is 'other'"})
    return code, (text.strip() if text else None)


def _record(db: Session, now: datetime, alert: Alert, plan: Plan | None, decision: str, actor: int | None,
            code: str | None, text: str | None) -> AlertDecision:
    row = AlertDecision(
        alert_id=alert.id, plan_id=plan.id if plan else None, decision=decision, actor_trainer_id=actor,
        reason_code=code, reason_text=text, sim_time=now,
    )
    db.add(row)
    log_event(db, now, "user", f"alert_{decision}" if decision != "approve" else "plan_approved",
              "plan" if plan else "alert", plan.id if plan else alert.id,
              {"alert_id": alert.id, "plan_id": plan.id if plan else None, "actor_trainer_id": actor,
               "reason_code": code, "reason_text": text})
    return row


def _plan_input(plan: Plan, alert: Alert) -> dict:
    return {"course_id": alert.course_id, "alert_id": alert.id, "actions": plan.actions}


def earliest_start(db: Session, plan_actions: list[dict]) -> datetime | None:
    """The earliest session time the plan touches (existing sessions it names, and the ones it creates)."""
    ids = set()
    starts: list[datetime] = []
    for a in plan_actions:
        for key in ("session_id", "from_session_id", "to_session_id"):
            if isinstance(a.get(key), int) and not isinstance(a.get(key), bool):
                ids.add(a[key])
        if a.get("type") == "add_session":
            try:
                starts.append(datetime.fromisoformat(str(a.get("start"))))
            except ValueError:
                pass
    if ids:
        starts.extend(db.scalars(select(TrainingSession.start_time).where(TrainingSession.id.in_(ids))))
    return min(starts) if starts else None


# --- Approve ----------------------------------------------------------------------------------------------
def _book(db: Session, session_id: int, driver_id: int) -> None:
    """Book a driver. The (session, driver) pair is unique, so an old cancelled row is reused."""
    existing = db.scalar(select(Enrollment).where(Enrollment.session_id == session_id, Enrollment.driver_id == driver_id))
    if existing is None:
        db.add(Enrollment(session_id=session_id, driver_id=driver_id, status="booked"))
    else:
        existing.status = "booked"


def _outbox_row(plan: Plan, now: datetime, change_type: str, session_id: int, payload: dict) -> CalendarOutbox:
    """One outbox row: "this session changed because of this plan, sync it"."""
    return CalendarOutbox(
        plan_id=plan.id, alert_id=plan.alert_id, session_id=session_id, change_type=change_type, op="upsert",
        source="plan_approval", payload=payload, created_at=now)


def _apply(db: Session, now: datetime, plan: Plan, parsed) -> list[CalendarOutbox]:
    """Make the plan's changes, in order, and return the outbox rows. Raises on any problem (caller rolls back)."""
    created: dict[str, int] = {}
    outbox: list[CalendarOutbox] = []

    def ref_to_id(ref) -> int:
        return created[ref] if isinstance(ref, str) else ref

    for a in parsed.actions:
        if isinstance(a, AddSession):
            row = TrainingSession(
                course_id=a.course_id, trainer_id=a.trainer_id, start_time=a.start, end_time=a.end, location=a.location,
                capacity=a.capacity, status="scheduled", source="agent",
            )
            db.add(row)
            db.flush()  # need the real id for later actions that point at new:N
            created[a.temp_ref] = row.id
            payload = {"session_id": row.id, "temp_ref": a.temp_ref, "course_id": a.course_id, "trainer_id": a.trainer_id,
                       "start": a.start.isoformat(), "end": a.end.isoformat(), "location": a.location, "capacity": a.capacity}
            outbox.append(_outbox_row(plan, now, "create_session", row.id, payload))
        elif isinstance(a, EnrollDriver):
            session_id = ref_to_id(a.session_id)
            _book(db, session_id, a.driver_id)
            outbox.append(_outbox_row(plan, now, "enroll_driver", session_id,
                                      {"driver_id": a.driver_id, "session_id": session_id}))
        elif isinstance(a, MoveDriver):
            source = db.scalar(select(Enrollment).where(
                Enrollment.session_id == a.from_session_id, Enrollment.driver_id == a.driver_id, Enrollment.status == "booked"))
            if source is None:
                raise RuntimeError(f"driver {a.driver_id} is not booked in session {a.from_session_id}")
            source.status = "cancelled"
            to_id = ref_to_id(a.to_session_id)
            _book(db, to_id, a.driver_id)
            move = {"driver_id": a.driver_id, "from_session_id": a.from_session_id, "to_session_id": to_id}
            # A move changes the roster of two sessions, so each one gets its own row.
            outbox.append(_outbox_row(plan, now, "move_driver", to_id, move))
            outbox.append(_outbox_row(plan, now, "move_driver", a.from_session_id, move))
        db.flush()
    db.add_all(outbox)
    db.flush()
    return outbox


def approval_result(db: Session, plan: Plan) -> dict:
    rows = list(db.scalars(select(CalendarOutbox).where(CalendarOutbox.plan_id == plan.id).order_by(CalendarOutbox.id)))
    return {
        "plan_id": plan.id, "alert_id": plan.alert_id, "status": plan.status,
        "outbox": [{"id": r.id, "change_type": r.change_type, "payload": r.payload, "status": r.status} for r in rows],
    }


def approve_plan(db: Session, plan_id: int, actor_trainer_id: int | None) -> dict:
    """Approve and apply a plan. Raises Conflict (409) with a reason when it can no longer be applied."""
    plan = _get_plan(db, plan_id)
    if plan.status == "approved":
        return {**approval_result(db, plan), "already_approved": True}  # idempotent: nothing is applied twice
    alert = _get_alert(db, plan.alert_id)
    actor = _check_actor(db, actor_trainer_id)
    if plan.status != "valid":
        raise Conflict(f"Plan {plan.id} is {plan.status}, so it cannot be approved")
    if alert.status != "plans_ready":
        raise Conflict(f"Alert {alert.id} is {alert.status}; plans can only be approved while it is plans_ready")

    now = get_sim_now(db)
    first = earliest_start(db, plan.actions)
    if first is not None and now >= first:
        plan.status = "expired"
        log_event(db, now, "system", "plan_expired", "plan", plan.id,
                  {"alert_id": alert.id, "earliest_session": first.isoformat(), "sim_now": now.isoformat()})
        db.commit()
        raise Conflict(f"Plan {plan.id} expired: its first session started on {first:%d %b %H:%M}, which is already in the past")

    parsed = parse_plan(_plan_input(plan, alert))
    if isinstance(parsed, Violation):
        violations = [parsed]
    else:
        violations = check_plan(db, parsed, now).violations
    if violations:
        plan.status = "stale"
        log_event(db, now, "system", "plan_stale", "plan", plan.id,
                  {"alert_id": alert.id, "violation_codes": [v.code for v in violations]})
        db.commit()
        raise Conflict(
            f"Plan {plan.id} is stale: the schedule changed since it was made and it now breaks {len(violations)} rule(s)",
            errors={f"{i}:{v.code}": v.message for i, v in enumerate(violations[:10])},
            extra={"reason": "stale", "violations": [v.model_dump(mode="json") for v in violations]},
        )

    try:
        outbox = _apply(db, now, plan, parsed)
        plan.status = "approved"
        for other in db.scalars(select(Plan).where(Plan.alert_id == alert.id, Plan.id != plan.id, Plan.status == "valid")):
            other.status = "superseded"
        set_alert_status(db, alert, "approved", f"plan {plan.id} approved")
        alert.updated_at = now
        _record(db, now, alert, plan, "approve", actor, None, None)
        log_event(db, now, "system", "plan_applied", "plan", plan.id, {
            "alert_id": alert.id, "changes": [{"change_type": o.change_type, **o.payload} for o in outbox]})
        db.commit()
    except Exception:
        db.rollback()  # the whole plan or nothing
        raise
    return {**approval_result(db, plan), "already_approved": False}


# --- Reject / dismiss ------------------------------------------------------------------------------------
def reject_plan(db: Session, plan_id: int, actor_trainer_id: int | None, reason_code: str | None, reason_text: str | None) -> dict:
    plan = _get_plan(db, plan_id)
    alert = _get_alert(db, plan.alert_id)
    actor = _check_actor(db, actor_trainer_id)
    code, text = _check_reason(reason_code, reason_text, required=True)
    if plan.status != "valid" or alert.status != "plans_ready":
        raise Conflict(f"Plan {plan.id} is {plan.status} (alert is {alert.status}), so it cannot be rejected now")
    now = get_sim_now(db)
    plan.status = "rejected"
    _record(db, now, alert, plan, "reject_plan", actor, code, text)
    db.flush()  # the query below must see this plan as rejected
    presented_left = db.scalar(select(Plan.id).where(
        Plan.alert_id == alert.id, Plan.status == "valid", Plan.rank <= settings.top_k).limit(1))
    if presented_left is None:
        for other in db.scalars(select(Plan).where(Plan.alert_id == alert.id, Plan.status == "valid")):
            other.status = "superseded"
        set_alert_status(db, alert, "rejected", "every recommended plan was rejected")
        alert.updated_at = now
    db.commit()
    return {"plan_id": plan.id, "alert_id": alert.id, "plan_status": plan.status, "alert_status": alert.status}


def reject_alert(db: Session, alert_id: int, actor_trainer_id: int | None, reason_code: str | None, reason_text: str | None) -> dict:
    alert = _get_alert(db, alert_id)
    actor = _check_actor(db, actor_trainer_id)
    code, text = _check_reason(reason_code, reason_text, required=True)
    if alert.status not in REJECTABLE_ALERT_STATUSES:
        raise Conflict(f"Alert {alert.id} is {alert.status}, so it cannot be rejected")
    now = get_sim_now(db)
    for plan in db.scalars(select(Plan).where(Plan.alert_id == alert.id, Plan.status == "valid")):
        plan.status = "rejected"
    set_alert_status(db, alert, "rejected", "alert rejected by a trainer")
    alert.updated_at = now
    _record(db, now, alert, None, "reject_alert", actor, code, text)
    db.commit()
    return {"alert_id": alert.id, "alert_status": alert.status}


def dismiss_alert(db: Session, alert_id: int, actor_trainer_id: int | None, reason_text: str | None = None) -> dict:
    """The trainer judges the alert not worth acting on. The weekly sync will not reopen it unless the risk gets worse."""
    alert = _get_alert(db, alert_id)
    actor = _check_actor(db, actor_trainer_id)
    if alert.status not in ACTIVE_STATUSES or alert.status == "generating":
        raise Conflict(f"Alert {alert.id} is {alert.status}, so it cannot be dismissed")
    now = get_sim_now(db)
    for plan in db.scalars(select(Plan).where(Plan.alert_id == alert.id, Plan.status == "valid")):
        plan.status = "superseded"
    set_alert_status(db, alert, "dismissed", "alert dismissed by a trainer")
    alert.updated_at = now
    _record(db, now, alert, None, "dismiss", actor, None, (reason_text or "").strip() or None)
    db.commit()
    return {"alert_id": alert.id, "alert_status": alert.status}
