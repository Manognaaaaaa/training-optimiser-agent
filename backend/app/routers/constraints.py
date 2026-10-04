from typing import Any

from fastapi import APIRouter, Body, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas.plan import CheckResult, Violation, parse_plan
from app.services.constraints import check_plan

router = APIRouter(prefix="/constraints", tags=["constraints"])


@router.post("/check", response_model=CheckResult)
def check(raw: Any = Body(...), db: Session = Depends(get_db)):
    """Dry run: is this plan allowed? Lists every rule it breaks.

    Writes nothing, not even an audit row (Phase 5 audits plans when it saves them).
    A body that is not a valid plan comes back as one SCHEMA_INVALID violation, not an error.
    """
    plan = parse_plan(raw)
    if isinstance(plan, Violation):
        return CheckResult.from_violations([plan])
    return check_plan(db, plan)
