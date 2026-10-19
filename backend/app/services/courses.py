"""Courses, edited together with their annual target (SIM_YEAR)."""
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.live.bus import publish_after_commit

from app.models import Course, TrainingSession, TrainingTarget
from app.schemas.courses import CourseCreate, CourseUpdate
from app.services.audit import apply_changes, log_event
from app.services.errors import Conflict, get_or_404
from app.services.pagination import PageParams, paginate
from app.services.sim_clock import get_sim_now
from app.simulator.rules import SIM_YEAR


def _to_out(course: Course, target: int | None) -> dict:
    return {
        "id": course.id, "code": course.code, "name": course.name, "duration_hours": course.duration_hours,
        "default_capacity": course.default_capacity, "is_mandatory": course.is_mandatory,
        "target_completions": target,
    }


def _target_row(db: Session, course_id: int) -> TrainingTarget | None:
    return db.scalars(
        select(TrainingTarget).where(TrainingTarget.course_id == course_id, TrainingTarget.year == SIM_YEAR)
    ).first()


def list_courses(db: Session, params: PageParams, *, q=None, is_mandatory=None):
    target = TrainingTarget.target_completions
    stmt = (
        select(Course, target)
        .outerjoin(TrainingTarget, (TrainingTarget.course_id == Course.id) & (TrainingTarget.year == SIM_YEAR))
    )
    if q:
        like = f"%{q.strip().lower()}%"
        stmt = stmt.where(Course.name.ilike(like) | Course.code.ilike(like))
    if is_mandatory is not None:
        stmt = stmt.where(Course.is_mandatory.is_(is_mandatory))
    sortable = {
        "code": Course.code, "name": Course.name, "duration_hours": Course.duration_hours,
        "default_capacity": Course.default_capacity, "is_mandatory": Course.is_mandatory,
        "target_completions": target,
    }
    rows, total = paginate(db, stmt, params, sortable, "code", Course.id)
    return [_to_out(c, t) for c, t in rows], total


def get_course(db: Session, course_id: int) -> dict:
    course = get_or_404(db, Course, course_id, "Course")
    row = _target_row(db, course.id)
    return _to_out(course, row.target_completions if row else None)


def _check_code_free(db: Session, code: str, exclude_id: int | None = None) -> None:
    clash = db.scalars(select(Course).where(Course.code == code)).first()
    if clash is not None and clash.id != exclude_id:
        raise Conflict(f"Course code {code} is already used by '{clash.name}'", {"code": "already in use"})


def create_course(db: Session, data: CourseCreate) -> dict:
    now = get_sim_now(db)
    _check_code_free(db, data.code)
    fields = data.model_dump(exclude={"target_completions"})
    course = Course(**fields)
    db.add(course)
    db.flush()
    if data.target_completions is not None:
        db.add(TrainingTarget(course_id=course.id, year=SIM_YEAR, target_completions=data.target_completions))
    log_event(db, now, "user", "course_created", "course", course.id, {**fields, "target_completions": data.target_completions})
    publish_after_commit(db, "master_data.changed", {"entity": "courses"})
    db.commit()
    return get_course(db, course.id)


def update_course(db: Session, course_id: int, data: CourseUpdate) -> dict:
    now = get_sim_now(db)
    course = get_or_404(db, Course, course_id, "Course")
    changes = {k: v for k, v in data.model_dump(exclude_unset=True).items() if v is not None}
    new_target = changes.pop("target_completions", None)
    if "code" in changes:
        _check_code_free(db, changes["code"], exclude_id=course.id)
    diff = apply_changes(course, changes)
    if new_target is not None:
        row = _target_row(db, course.id)
        if row is None:
            db.add(TrainingTarget(course_id=course.id, year=SIM_YEAR, target_completions=new_target))
            diff["target_completions"] = [None, new_target]
        elif row.target_completions != new_target:
            diff["target_completions"] = [row.target_completions, new_target]
            row.target_completions = new_target
    if diff:
        log_event(db, now, "user", "course_updated", "course", course.id, diff)
        publish_after_commit(db, "master_data.changed", {"entity": "courses"})
        db.commit()
    return get_course(db, course.id)


def delete_course(db: Session, course_id: int) -> None:
    now = get_sim_now(db)
    course = get_or_404(db, Course, course_id, "Course")
    n = db.scalar(select(func.count()).select_from(TrainingSession).where(TrainingSession.course_id == course.id)) or 0
    if n:
        raise Conflict(f"{course.code} has {n} sessions, so it cannot be deleted. Cancel and keep it for the history.")
    for row in db.scalars(select(TrainingTarget).where(TrainingTarget.course_id == course.id)):
        db.delete(row)
    log_event(db, now, "user", "course_deleted", "course", course.id, {"code": course.code, "name": course.name})
    db.delete(course)
    publish_after_commit(db, "master_data.changed", {"entity": "courses"})
    db.commit()
