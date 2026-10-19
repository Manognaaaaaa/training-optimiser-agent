"""Approve or reject one plan. All the rules live in services/decisions.py."""
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.services import decisions

router = APIRouter(prefix="/plans", tags=["plans"])


class ApproveRequest(BaseModel):
    actor_trainer_id: int | None = None


class RejectRequest(BaseModel):
    actor_trainer_id: int | None = None
    reason_code: str | None = None
    reason_text: str | None = None


@router.post("/{plan_id}/approve")
def approve(plan_id: int, body: ApproveRequest, db: Session = Depends(get_db)):
    """Re-check the plan against today's data, apply it in one transaction and queue the calendar changes."""
    return decisions.approve_plan(db, plan_id, body.actor_trainer_id)


@router.post("/{plan_id}/reject")
def reject(plan_id: int, body: RejectRequest, db: Session = Depends(get_db)):
    return decisions.reject_plan(db, plan_id, body.actor_trainer_id, body.reason_code, body.reason_text)
