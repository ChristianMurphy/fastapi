"""The client stops reading after one event and then disconnects.

Drives the app in-process, so the disconnect lands at a known point.
Needs pytest and anyio's pytest plugin (installed with anyio).
"""

from collections.abc import AsyncIterator
from typing import Annotated, Any

import anyio
import pytest
from close_streams import StreamTracker, closing_streams
from fastapi import Depends, FastAPI
from fastapi.sse import EventSourceResponse

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def make_app(log: list[str], *, guarded: bool) -> FastAPI:
    app = FastAPI()

    async def session() -> AsyncIterator[dict[str, bool]]:
        state = {"open": True}
        try:
            yield state
        except BaseException as exc:
            log.append(f"session saw {type(exc).__name__}")
            raise
        finally:
            state["open"] = False
            log.append("session closed")

    Session = Annotated[dict[str, bool], Depends(session)]

    async def produce(state: dict[str, bool]) -> AsyncIterator[dict[str, int]]:
        try:
            for i in range(1000):
                yield {"i": i}
        finally:
            log.append(f"stream cleanup, session open: {state['open']}")

    if guarded:

        async def close_streams(state: Session) -> AsyncIterator[StreamTracker]:
            async with closing_streams() as track:
                yield track

        @app.get("/", response_class=EventSourceResponse)
        async def events(
            state: Session, track: Annotated[StreamTracker, Depends(close_streams)]
        ) -> AsyncIterator[dict[str, int]]:
            stream = track(produce(state))
            async for event in stream:
                yield event

    else:

        @app.get("/", response_class=EventSourceResponse)
        async def events(state: Session) -> AsyncIterator[dict[str, int]]:
            async for event in produce(state):
                yield event

    return app


async def disconnect_after_first_event(app: FastAPI) -> BaseException | None:
    gone = anyio.Event()
    requested = False
    events_sent = 0

    async def receive() -> dict[str, Any]:
        nonlocal requested
        if not requested:
            requested = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await gone.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        nonlocal events_sent
        if message["type"] == "http.response.body" and message.get("body"):
            if events_sent == 1:
                # The client has stopped reading; let the server fill its
                # buffers, then disconnect while this send is still blocked.
                for _ in range(20):
                    await anyio.sleep(0)
                gone.set()
                await anyio.sleep_forever()
            events_sent += 1

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"test")],
        "client": ("client", 1),
        "server": ("test", 80),
    }
    try:
        with anyio.fail_after(5):
            await app(scope, receive, send)
    except Exception as exc:
        return exc
    return None


async def test_guarded_stream_cleans_up_before_the_session_closes() -> None:
    log: list[str] = []
    raised = await disconnect_after_first_event(make_app(log, guarded=True))
    assert raised is None
    assert log == ["stream cleanup, session open: True", "session closed"]


async def test_unguarded_stream_shows_the_problem() -> None:
    """Documents current FastAPI behaviour. When this fails, the upstream
    discussions were probably resolved and the workaround can go."""
    log: list[str] = []
    raised = await disconnect_after_first_event(make_app(log, guarded=False))
    assert isinstance(raised, ExceptionGroup)
    assert log == ["session saw ExceptionGroup", "session closed"]
