"""Deterministic disconnect harness for streaming endpoints with yield dependencies.

Drives the ASGI app in-process. The client "disconnects" right after the k-th
body chunk has been sent, for every k. Records the order of events and checks:

  once     every entered dependency exits exactly once
  gen_once the stream generator's cleanup (finally) runs exactly once, before
           the ASGI call returns
  order    request-scope dependencies are still open when the generator's
           cleanup runs

Function-scope dependencies exit before a streaming body starts. The docs
describe that as the purpose of scope="function", so it is reported as a note
(fn_closed), not as a violation.

consumer="slow": the client stops reading after k chunks, so the server's send
blocks, and then disconnects. consumer="fast": the client reads k chunks and
disconnects at once.
"""

import gc
import itertools
import sys
from typing import Annotated, Any

import anyio
import fastapi
from fastapi import Depends, FastAPI
from fastapi.responses import StreamingResponse

try:
    from fastapi.sse import EventSourceResponse
except Exception:  # pragma: no cover
    EventSourceResponse = None

N_ITEMS = 6


def make_app(endpoint: str, dep: str, scope: str, park: str, log: list):
    state = {"open": False}

    def enter():
        state["open"] = True
        log.append("dep enter")

    def leave():
        state["open"] = False
        log.append("dep exit")

    if dep == "async":

        async def resource():
            enter()
            try:
                yield state
            finally:
                leave()
    else:

        def resource():
            enter()
            try:
                yield state
            finally:
                leave()

    Dep = Annotated[dict, Depends(resource, scope=scope)]

    app = FastAPI()
    if endpoint in ("sse_async", "raw_async"):
        cls = EventSourceResponse if endpoint == "sse_async" else StreamingResponse

        @app.get("/", response_class=cls)
        async def ep(s: Dep):
            log.append(f"gen start dep_open={s['open']}")
            try:
                for i in range(N_ITEMS):
                    if park == "await":
                        await anyio.sleep(0)
                    yield f"item{i}"
            finally:
                log.append(f"gen cleanup dep_open={s['open']}")
    elif endpoint in ("sse_sync", "raw_sync"):
        cls = EventSourceResponse if endpoint == "sse_sync" else StreamingResponse

        @app.get("/", response_class=cls)
        def ep(s: Dep):
            log.append(f"gen start dep_open={s['open']}")
            try:
                for i in range(N_ITEMS):
                    yield f"item{i}"
            finally:
                log.append(f"gen cleanup dep_open={s['open']}")
    elif endpoint == "returned":

        async def agen(s):
            log.append(f"gen start dep_open={s['open']}")
            try:
                for i in range(N_ITEMS):
                    if park == "await":
                        await anyio.sleep(0)
                    yield f"item{i}"
            finally:
                log.append(f"gen cleanup dep_open={s['open']}")

        @app.get("/")
        async def ep(s: Dep):
            return StreamingResponse(agen(s))
    else:
        raise ValueError(endpoint)
    return app


async def drive(
    app, k: int | None, spec: str, log: list, consumer: str = "fast"
) -> None:
    """k=None: read everything. Otherwise disconnect after the k-th body chunk."""
    gone = anyio.Event()
    sent_request = False
    bodies = 0

    async def receive():
        nonlocal sent_request
        if not sent_request:
            sent_request = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await gone.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        nonlocal bodies
        if gone.is_set():
            if spec == "2.4":
                raise OSError("client disconnected")
            return
        if message["type"] == "http.response.start":
            if k == 0:
                gone.set()
            return
        if message["type"] == "http.response.body" and message.get("body"):
            if consumer == "slow" and k is not None and bodies >= k:
                # the client has stopped reading: this send blocks, then it leaves
                for _ in range(20):
                    await anyio.sleep(0)
                log.append("client gone while send blocked")
                gone.set()
                await anyio.sleep_forever()
            bodies += 1
            log.append(f"sent {bodies}")
            if consumer == "fast" and k is not None and bodies >= k:
                gone.set()

    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": spec},
        "http_version": "1.1", "method": "GET", "scheme": "http", "path": "/",
        "raw_path": b"/", "query_string": b"", "root_path": "",
        "headers": [(b"host", b"t")], "client": ("c", 1), "server": ("t", 80),
    }  # fmt: skip
    try:
        with anyio.fail_after(5):
            await app(scope, receive, send)
    except TimeoutError:
        log.append("HANG")
    except BaseException as e:  # noqa: BLE001
        log.append(f"raised {type(e).__name__}")
    log.append("app returned")
    for _ in range(3):
        gc.collect()
        await anyio.sleep(0)
    log.append("after gc")


def check(log: list, scope: str) -> list[str]:
    bad = []
    upto = log.index("app returned")
    inside = log[:upto]
    if "HANG" in log:
        bad.append("hang")
    if (
        inside.count("dep enter") != inside.count("dep exit")
        or inside.count("dep exit") > 1
    ):
        bad.append("once")
    started = any(e.startswith("gen start") for e in log)
    cleanups = [e for e in log if e.startswith("gen cleanup")]
    cleanups_inside = [e for e in inside if e.startswith("gen cleanup")]
    if started and len(cleanups) != 1:
        bad.append("gen_once")
    elif started and len(cleanups_inside) != 1:
        bad.append("gen_late")
    if scope == "request" and any(e.endswith("dep_open=False") for e in cleanups):
        bad.append("order")
    return bad


async def main(verbose: bool) -> int:
    endpoints = ["sse_async", "sse_sync", "raw_async", "raw_sync", "returned"]
    rows, failures = [], 0
    for endpoint, dep, scope, park, consumer in itertools.product(
        endpoints,
        ["async", "sync"],
        ["request", "function"],
        ["yield", "await"],
        ["fast", "slow"],
    ):
        if endpoint.endswith("_sync") and park == "await":
            continue
        for k in [None, 0, 1, 2, 3]:
            if k is None and consumer == "slow":
                continue
            log: list = []
            await drive(
                make_app(endpoint, dep, scope, park, log), k, SPEC, log, consumer
            )
            bad = check(log, scope)
            rows.append((endpoint, dep, scope, park, consumer, k, bad, log))
            failures += bool(bad)
    summary: dict = {}
    for endpoint, dep, scope, park, consumer, k, bad, log in rows:
        if scope == "function":
            continue  # same shape as request scope apart from the documented early exit
        summary.setdefault((endpoint, consumer, tuple(bad)), []).append(
            (dep, park, k, log)
        )
    print(
        f"fastapi {fastapi.__version__}, ASGI spec {SPEC}: {len(rows)} cases, {failures} with a violation"
    )
    print("  request-scope dependencies:")
    for (endpoint, consumer, bad), cases in sorted(summary.items()):
        ks = sorted({str(c[2]) for c in cases})
        print(
            f"  {endpoint:10} client={consumer:4} {','.join(bad) or 'ok':16} {len(cases):3} cases  k={','.join(ks)}"
        )
        if verbose and bad:
            dep, park, k, log = cases[0]
            print(f"      e.g. dep={dep} park={park} k={k}: {log}")
    return failures


SPEC = "2.3"  # what uvicorn reports

if __name__ == "__main__":
    if "--spec-2.4" in sys.argv:
        SPEC = "2.4"
    anyio.run(main, "-v" in sys.argv)
