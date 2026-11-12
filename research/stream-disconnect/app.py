"""Confirm app: an SSE endpoint whose generator and dependencies log cleanup.

MODE=burst  items are ready at once, so the producer is usually blocked sending
MODE=wait   50 ms between items, so the generator is usually awaiting (control)
GUARD=1     apply the app-side workaround (see close_streams)
"""

import asyncio
import os
import sys
from collections.abc import AsyncGenerator, Callable
from typing import Annotated

import anyio
from fastapi import Depends, FastAPI
from fastapi.sse import EventSourceResponse

MODE = os.environ.get("MODE", "burst")
GUARD = os.environ.get("GUARD") == "1"
REQ = 0


def say(msg: str) -> None:
    print(f"LOG {msg}", file=sys.stderr, flush=True)


async def session():
    global REQ
    REQ += 1
    state = {"open": True, "req": REQ}
    outcome = "commit"
    try:
        yield state
    except BaseException as e:
        outcome = f"rollback({type(e).__name__})"
        raise
    finally:
        await asyncio.sleep(0.01)  # stands in for closing a connection
        state["open"] = False
        say(f"req {state['req']} dep exit outcome={outcome}")


Session = Annotated[dict, Depends(session)]


async def close_streams(s: Session):
    """Workaround: close registered generators before the session exits.

    Depends on the session, so it exits first.
    """
    streams: list[AsyncGenerator] = []
    try:
        yield streams.append
    except BaseExceptionGroup as group:
        # FastAPI's SSE keepalive task raises BrokenResourceError when the client
        # leaves while it is blocked sending. Drop that one; keep anything else.
        rest = group.split(anyio.BrokenResourceError)[1]
        if rest is not None:
            raise rest from None
    finally:
        for stream in streams:
            await stream.aclose()


app = FastAPI()


async def events(s: dict):
    try:
        for i in range(100000):
            if MODE == "wait":
                await asyncio.sleep(0.05)
            yield {"i": i, "pad": "x" * 2000}
    finally:
        task = asyncio.current_task().get_name().rsplit(".", 1)[-1]
        say(
            f"req {s['req']} gen cleanup dep_open={s['open']} task={task} during_req={REQ}"
        )


if GUARD:

    @app.get("/stream", response_class=EventSourceResponse)
    async def stream(s: Session, track: Annotated[Callable, Depends(close_streams)]):
        inner = events(s)
        track(inner)
        async for item in inner:
            yield item

else:

    @app.get("/stream", response_class=EventSourceResponse)
    async def stream(s: Session):
        async for item in events(s):
            yield item
