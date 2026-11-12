"""Close SSE generators before their dependencies exit.

Works around two FastAPI behaviours when a client disconnects from an
`EventSourceResponse` endpoint while the endpoint generator is suspended at a
`yield` (FastAPI 0.135.0 to 0.162.5 at least; upstream discussions 16346 and
15725):

1. Nothing closes the generator, so its `finally` and `async with` blocks run
   only when the garbage collector finalizes it, after `yield` dependencies
   such as a database session have exited.
2. FastAPI's keepalive task raises `anyio.BrokenResourceError`, which reaches
   every `yield` dependency as an `ExceptionGroup` and is logged by the server
   as "Exception in ASGI application".

Usage: make the dependency below depend on every resource the stream uses, so
that it exits first, then register the generator that does the work.

    async def close_streams(session: SessionDep) -> AsyncIterator[StreamTracker]:
        async with closing_streams() as track:
            yield track

    @app.get("/events", response_class=EventSourceResponse)
    async def events(session: SessionDep, track: Annotated[StreamTracker, Depends(close_streams)]):
        stream = track(produce_events(session))
        async for event in stream:
            yield event

Put the cleanup that matters in `produce_events`, not in the endpoint body: the
endpoint generator itself is still finalized late.

Needs Python 3.11 or newer (exception groups). Remove this when the upstream
discussions are resolved.
"""

from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Protocol, TypeVar

import anyio

G = TypeVar("G", bound=AsyncGenerator[Any, None])


class StreamTracker(Protocol):
    def __call__(self, stream: G, /) -> G: ...


@asynccontextmanager
async def closing_streams() -> AsyncIterator[StreamTracker]:
    streams: list[AsyncGenerator[Any, None]] = []

    def track(stream: G) -> G:
        streams.append(stream)
        return stream

    try:
        yield track
    except BaseExceptionGroup as group:
        # Drop the keepalive task's error; re-raise anything else unchanged.
        _, rest = group.split(anyio.BrokenResourceError)
        if rest is not None:
            raise rest from None
    finally:
        # Shielded: the request may already be cancelled, and cleanup must finish.
        with anyio.CancelScope(shield=True):
            for stream in reversed(streams):
                await stream.aclose()
