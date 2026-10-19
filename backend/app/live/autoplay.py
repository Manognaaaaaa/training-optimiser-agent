"""Simulation auto-play: advance the simulated clock on a timer so the app looks alive in a demo.

It pauses itself (and publishes ``sim.autoplay_paused`` with a reason) when a planner run is active, when the
simulated year ends, or when an advance fails.
"""
import asyncio
import logging
import threading
from collections.abc import Callable

from sqlalchemy.orm import Session

from app.live.bus import bus

log = logging.getLogger(__name__)
MIN_INTERVAL_SECONDS = 3
STEP_DAYS = {"day": 1, "week": 7}


class _ThreadBackground:
    """Stand-in for FastAPI's BackgroundTasks: runs the planner in a daemon thread."""

    def add_task(self, fn, *args) -> None:
        threading.Thread(target=fn, args=args, daemon=True).start()


def advance_once(session_factory: Callable[[], Session], days: int) -> str | None:
    """Advance the sim by ``days``. Returns a pause reason, or None to keep going. Runs in a worker thread."""
    from app.config import settings
    from app.llm.planner import active_run_exists
    from app.routers.sim import _auto_plan
    from app.services.sim_clock import get_sim_now
    from app.simulator import engine as sim_engine
    from app.simulator.rules import SIM_END

    with session_factory() as db:
        if active_run_exists(db):
            return "planner_running"
        if get_sim_now(db) >= SIM_END:
            return "year_end"
        summary = sim_engine.advance(db, days)
        if settings.auto_plan_on_high_risk:
            _auto_plan(db, _ThreadBackground(), session_factory)
        return "year_end" if summary.hit_year_end else None


class Autoplay:
    def __init__(self, session_factory: Callable[[], Session] | None = None,
                 step_fn: Callable[[int], str | None] | None = None) -> None:
        self._factory = session_factory
        self._step_fn = step_fn  # tests inject a fake: (days) -> pause reason | None
        self._task: asyncio.Task | None = None
        self.enabled = False
        self.step = "day"
        self.interval_seconds: float = 5
        self.paused_reason: str | None = None
        self.steps_done = 0

    def state(self) -> dict:
        return {"enabled": self.enabled, "step": self.step, "interval_seconds": self.interval_seconds,
                "paused_reason": self.paused_reason, "steps_done": self.steps_done}

    def _step(self, days: int) -> str | None:
        if self._step_fn is not None:
            return self._step_fn(days)
        from app.database import SessionLocal

        return advance_once(self._factory or SessionLocal, days)

    async def start(self, step: str = "day", interval_seconds: float = 5) -> dict:
        self.step, self.interval_seconds = step, interval_seconds
        self.enabled, self.paused_reason = True, None
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="sim-autoplay")
        bus.publish("sim.autoplay_changed", self.state())
        return self.state()

    async def stop(self) -> dict:
        self.enabled = False
        task, self._task = self._task, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        bus.publish("sim.autoplay_changed", self.state())
        return self.state()

    def _pause(self, reason: str) -> None:
        self.enabled, self.paused_reason = False, reason
        bus.publish("sim.autoplay_paused", {"reason": reason})

    async def _run(self) -> None:
        while self.enabled:
            await asyncio.sleep(self.interval_seconds)
            if not self.enabled:
                return
            try:
                reason = await asyncio.to_thread(self._step, STEP_DAYS[self.step])
            except Exception as err:
                log.exception("autoplay step failed")
                self._pause(f"error: {err.__class__.__name__}: {err}")
                return
            if reason in (None, "year_end"):
                self.steps_done += 1  # the advance happened (year_end means it ran to the end)
            if reason is not None:
                self._pause(reason)
                return


autoplay = Autoplay()
