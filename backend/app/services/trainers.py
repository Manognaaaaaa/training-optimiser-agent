"""Trainers and their weekly load."""
from collections import Counter
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.live.bus import publish_after_commit

from app.models import Trainer, TrainingSession
from app.schemas.trainers import TrainerCreate, TrainerUpdate
from app.services.audit import apply_changes, log_event
from app.services.errors import Conflict, get_or_404
from app.services.pagination import PageParams, paginate
from app.services.sim_clock import get_sim_now
from app.simulator.rules import week_start

SORTABLE = {"name": Trainer.name, "max_sessions_per_week": Trainer.max_sessions_per_week}


def _to_out(trainer: Trainer, this_week: int, warnings: list[str] | None = None) -> dict:
    return {
        "id": trainer.id, "name": trainer.name, "max_sessions_per_week": trainer.max_sessions_per_week,
        "sessions_this_week": this_week, "warnings": warnings or [],
    }


def _week_bounds(now: datetime) -> tuple[datetime, datetime]:
    ws = datetime.combine(week_start(now.date()), datetime.min.time())
    return ws, ws + timedelta(days=7)


def _this_week_counts(db: Session, now: datetime) -> dict[int, int]:
    """Sessions per trainer in the week containing sim now (one grouped query)."""
    ws, we = _week_bounds(now)
    rows = db.execute(
        select(TrainingSession.trainer_id, func.count())
        .where(TrainingSession.status != "cancelled", TrainingSession.start_time >= ws, TrainingSession.start_time < we)
        .group_by(TrainingSession.trainer_id)
    ).all()
    return dict(rows)


def list_trainers(db: Session, params: PageParams, *, q=None):
    now = get_sim_now(db)
    stmt = select(Trainer)
    if q:
        stmt = stmt.where(Trainer.name.ilike(f"%{q.strip().lower()}%"))
    rows, total = paginate(db, stmt, params, SORTABLE, "name", Trainer.id)
    counts = _this_week_counts(db, now)
    return [_to_out(r[0], counts.get(r[0].id, 0)) for r in rows], total


def get_trainer(db: Session, trainer_id: int) -> dict:
    trainer = get_or_404(db, Trainer, trainer_id, "Trainer")
    return _to_out(trainer, _this_week_counts(db, get_sim_now(db)).get(trainer.id, 0))


def create_trainer(db: Session, data: TrainerCreate) -> dict:
    now = get_sim_now(db)
    trainer = Trainer(**data.model_dump())
    db.add(trainer)
    db.flush()
    log_event(db, now, "user", "trainer_created", "trainer", trainer.id, data.model_dump())
    publish_after_commit(db, "master_data.changed", {"entity": "trainers"})
    db.commit()
    return get_trainer(db, trainer.id)


def _overloaded_weeks(db: Session, trainer: Trainer, now: datetime) -> list[str]:
    """Future weeks (from the current one on) where the trainer already has more sessions than the limit."""
    ws, _ = _week_bounds(now)
    starts = db.scalars(
        select(TrainingSession.start_time).where(
            TrainingSession.trainer_id == trainer.id, TrainingSession.status != "cancelled", TrainingSession.start_time >= ws
        )
    )
    per_week = Counter(week_start(s.date()) for s in starts)
    return [
        f"Week starting {w:%Y-%m-%d} already has {n} sessions (new limit {trainer.max_sessions_per_week})"
        for w, n in sorted(per_week.items())
        if n > trainer.max_sessions_per_week
    ]


def update_trainer(db: Session, trainer_id: int, data: TrainerUpdate) -> dict:
    now = get_sim_now(db)
    trainer = get_or_404(db, Trainer, trainer_id, "Trainer")
    changes = {k: v for k, v in data.model_dump(exclude_unset=True).items() if v is not None}
    diff = apply_changes(trainer, changes)
    warnings: list[str] = []
    if diff:
        log_event(db, now, "user", "trainer_updated", "trainer", trainer.id, diff)
        publish_after_commit(db, "master_data.changed", {"entity": "trainers"})
        db.commit()
        if "max_sessions_per_week" in diff:
            warnings = _overloaded_weeks(db, trainer, now)
    out = _to_out(trainer, _this_week_counts(db, now).get(trainer.id, 0), warnings)
    return out


def delete_trainer(db: Session, trainer_id: int) -> None:
    now = get_sim_now(db)
    trainer = get_or_404(db, Trainer, trainer_id, "Trainer")
    n = db.scalar(
        select(func.count()).select_from(TrainingSession).where(
            TrainingSession.trainer_id == trainer.id, TrainingSession.status.in_(["scheduled", "completed"])
        )
    ) or 0
    if n:
        raise Conflict(f"{trainer.name} has {n} scheduled or completed sessions. Reassign them to another trainer first.")
    log_event(db, now, "user", "trainer_deleted", "trainer", trainer.id, {"name": trainer.name})
    db.delete(trainer)
    publish_after_commit(db, "master_data.changed", {"entity": "trainers"})
    db.commit()
