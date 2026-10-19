"""GET /api/live/stream: Server-Sent Events. The payloads are tiny (ids and counts); the client refetches the data."""
import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable

from fastapi import APIRouter, Header, Request
from fastapi.responses import StreamingResponse

from app.config import settings
from app.live.bus import LiveEvent, bus

router = APIRouter(prefix="/live", tags=["live"])


def format_event(ev: LiveEvent) -> str:
    return f"id: {ev.id}\nevent: {ev.type}\ndata: {json.dumps({**ev.data, '_ts': ev.ts})}\n\n"


async def event_stream(
    last_event_id: int | None, heartbeat_seconds: float, is_disconnected: Callable[[], Awaitable[bool]] | None = None,
) -> AsyncIterator[str]:
    """Yield SSE text. Always unsubscribes when the client goes away (the generator is closed or cancelled)."""
    bus.attach_loop(asyncio.get_running_loop())
    sub = bus.subscribe(last_event_id)
    try:
        yield "retry: 3000\n\n"
        while True:
            try:
                ev = await asyncio.wait_for(sub.queue.get(), timeout=heartbeat_seconds)
            except asyncio.TimeoutError:
                if is_disconnected is not None and await is_disconnected():
                    return
                yield "event: heartbeat\ndata: {}\n\n"
                continue
            yield format_event(ev)
    finally:
        bus.unsubscribe(sub)


def parse_last_event_id(value: str | None) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except ValueError:
        return None


@router.get("/stream")
async def stream(request: Request, last_event_id: str | None = Header(default=None)):
    gen = event_stream(parse_last_event_id(last_event_id), settings.live_heartbeat_seconds, request.is_disconnected)
    return StreamingResponse(gen, media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"})
