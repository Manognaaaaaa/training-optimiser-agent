"""Google Calendar sync API: status, retries, backfill, reconcile and reset. DB -> Google only."""
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.calendar import ops
from app.calendar.factory import CalendarBackend, get_backend
from app.calendar.sync import sync_service
from app.database import get_db

router = APIRouter(prefix="/calendar", tags=["calendar sync"])


class BackfillRequest(BaseModel):
    from_date: datetime
    to_date: datetime


class ResetRequest(BaseModel):
    confirm: str = ""


def backend_dep() -> CalendarBackend:
    return get_backend()


@router.get("/sync/status")
def sync_status(db: Session = Depends(get_db), backend: CalendarBackend = Depends(backend_dep)):
    return ops.status_summary(db, backend)


@router.get("/sync/failed")
def sync_failed(db: Session = Depends(get_db)):
    return ops.failed_rows(db)


@router.post("/sync/retry/{outbox_id}")
def retry(outbox_id: int, db: Session = Depends(get_db)):
    row = ops.retry_one(db, outbox_id)
    sync_service.kick()
    return row


@router.post("/sync/retry-all-failed")
def retry_all(db: Session = Depends(get_db)):
    count = ops.retry_all_failed(db)
    sync_service.kick()
    return {"requeued": count}


@router.post("/sync/session/{session_id}", status_code=202)
def sync_session(session_id: int, db: Session = Depends(get_db)):
    row = ops.enqueue_session(db, session_id)
    sync_service.kick()
    return row


@router.post("/backfill", status_code=202)
def backfill(body: BackfillRequest, db: Session = Depends(get_db)):
    result = ops.backfill(db, body.from_date, body.to_date)
    sync_service.kick()
    return result


@router.get("/reconcile")
def reconcile(
    from_: datetime = Query(alias="from"), to: datetime = Query(), db: Session = Depends(get_db), backend: CalendarBackend = Depends(backend_dep),
):
    """Read only for Google. Query parameters are ``from`` and ``to``."""
    return ops.reconcile(db, backend, from_, to)


@router.post("/reset")
def reset(body: ResetRequest, db: Session = Depends(get_db), backend: CalendarBackend = Depends(backend_dep)):
    return ops.reset_calendar(db, backend, body.confirm)
