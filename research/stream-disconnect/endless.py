"""Endless SSE stream, client disconnects after k events. Does the request end?"""

import itertools
import sys

import anyio
import disconnect_harness as h

h.N_ITEMS = 10**9


async def main() -> None:
    hang = total = late = 0
    for dep, park, consumer, k in itertools.product(
        ["async"], ["yield", "await"], ["fast", "slow"], [0, 1, 3]
    ):
        log: list = []
        app = h.make_app("sse_async", dep, "request", park, log)
        with anyio.move_on_after(8):
            await h.drive(app, k, "2.3", log, consumer)
        total += 1
        hung = "HANG" in log or "app returned" not in log
        hang += hung
        late += (not hung) and bool(h.check(log, "request"))
        if "-v" in sys.argv and hung:
            print("   hang:", dep, park, consumer, k)
    print(
        f"endless SSE: {total} cases, request never ended in {hang}, cleanup late in {late}"
    )


anyio.run(main)
