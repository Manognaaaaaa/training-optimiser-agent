from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import models  # noqa: F401  (registers all tables on Base.metadata)
from app.config import settings
from app.database import Base, SessionLocal, engine
from app.llm.base import LLMError
from app.llm.planner import mark_stale_runs_failed
from app.routers import (
    alerts, calendar, constraints, courses, drivers, health, planner, plans, sessions, sim, tracking, trainers,
)
from app.services.errors import ApiError


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Create any missing tables when the server starts, and fail planner runs a crash left half-done."""
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        mark_stale_runs_failed(db)
    yield


app = FastAPI(title=settings.app_name, lifespan=lifespan)

# Lets the React app (port 5173) call this API (port 8000)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_origin],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)



@app.exception_handler(ApiError)
async def api_error_handler(request: Request, exc: ApiError):
    """404 / 409 / 422 raised by services. The body always has a readable ``detail`` string."""
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail, "errors": exc.errors, **exc.extra})


@app.exception_handler(LLMError)
async def llm_error_handler(request: Request, exc: LLMError):
    """No key configured -> 503 with a clear message. Any other LLM failure -> 502."""
    status = 503 if exc.kind == "unavailable" else 502
    return JSONResponse(status_code=status, content={"detail": exc.message, "errors": {}, "kind": exc.kind})


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    """Turn pydantic's list of errors into one readable string plus a {field: message} map for forms."""
    errors: dict[str, str] = {}
    for err in exc.errors():
        loc = [str(part) for part in err["loc"] if part not in ("body", "query", "path")]
        errors[".".join(loc) or "request"] = err["msg"]
    detail = "; ".join(f"{field}: {msg}" for field, msg in errors.items())
    return JSONResponse(status_code=422, content={"detail": detail, "errors": errors})


app.include_router(health.router, prefix="/api")
app.include_router(sim.router, prefix="/api")
for _router in (
    drivers.router, trainers.router, courses.router, sessions.router, calendar.router, tracking.router, alerts.router,
    constraints.router, planner.router, plans.router,
):
    app.include_router(_router, prefix="/api")
