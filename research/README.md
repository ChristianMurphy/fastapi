# Research notes: models, harnesses and tracker

Working notes, not part of FastAPI and not proposed upstream. Nothing here
has been posted anywhere.

## Files

In `dependency-lifecycle/`:

- `deps.qnt`: a Quint model of one request with three chained dependencies,
  each a generator or a plain callable, each edge with a declared scope, and
  cancellation after the endpoint or after the response.
- `scope_transitive.py`: the confirm script for the one violation the model
  produced.
- `test_dependency_graph.py`: a Hypothesis test that generates dependency
  graphs (sync, async, generator, async generator, `use_cache` on or off) and
  compares call count, call order and teardown order with a small reference
  model.

In `stream-disconnect/`:

- `disconnect_harness.py`: drives the ASGI app in-process and disconnects
  the client after every possible chunk, for SSE, generator and returned
  streaming endpoints, with a fast or a stalled client.
- `app.py` and `run.sh`: a real uvicorn server (uvloop, httptools) and a
  client that drops the socket after 0.3 s. `MODE=burst` or `MODE=wait`,
  `GUARD=1` applies the app-side workaround.
- `stream.qnt`: a Quint model of SSE teardown on disconnect, as it is and
  with a proposed order (`--main=current` or `--main=design`).

## Running

```
npm i -g @informalsystems/quint
quint run deps.qnt --backend=typescript --invariant=exactlyOnce --max-samples=5000 --max-steps=8
quint run deps.qnt --backend=typescript --invariant=exitBeforeSubs --max-samples=5000 --max-steps=8 --seed=1
python scope_transitive.py
pytest -o addopts="" test_dependency_graph.py   # needs hypothesis

python stream-disconnect/disconnect_harness.py -v
stream-disconnect/run.sh python burst 8811 20     # then: wait, and GUARD=1
quint run stream-disconnect/stream.qnt --main=current --step=step_ --backend=typescript --invariant=cleanupBeforeDeps
```

## Results on 0.162.5 (2026-11-12)

- `exactlyOnce` holds in the model, with and without cancellation. The model
  describes the code as read; no harness has checked cancellation against
  the running code yet.
- `test_dependency_graph.py` passes 300 examples in about 2 s. Two planted
  mutations in `solve_dependencies` (ignore `use_cache=False`; never store
  in the cache) each fail it within a second, shrunk to a one-node graph.
- `exitBeforeSubs` is violated. See the tracker entry.

## Tracker

### Scope rule is checked for direct sub-dependencies only

- Status: held. Not reported.
- What happens: a request-scope generator dependency depends on a plain
  dependency, which depends on a generator declared with
  `scope="function"`. The app starts, the function-scope dependency exits
  before the response, and the request-scope one runs its exit code after
  it. `scope_transitive.py` prints `session.open=False` in that exit code,
  3 of 3 runs.
- Control: the same two generators wired directly raise
  `DependencyScopeError` at startup.
- Documented rule: "any dependency needs to be able to run its exit code
  before the sub-dependencies" (tutorial, dependencies with yield, scope for
  sub-dependencies).
- Cause, by reading: the check in `get_dependant` compares a dependency only
  with its direct sub-dependency.
- Origin: long-standing since scopes were added in 0.121.0 (PR 14262). The
  script gives the same output on 0.121.0, 0.151.0 and 0.162.5.
- Rank: visible failure at most, and only when exit code touches the closed
  resource. No silent loss.
- Preferred fix if it is ever reported: make the startup check transitive.
- Closest threads, none a duplicate: discussions 16146 (one dependency
  declared with both scopes runs twice), 14296 (function-scope cleanup after
  the response for router-level dependencies, fixed in 0.121.1), 14552
  (dependencies exit on client disconnect while streaming). Issues and PRs
  searched under four phrasings; discussions under five, first page each.

### Header title differs between declaration forms

- Status: held, cosmetic. A header declared through a parameter model gets
  the OpenAPI title "Item Id"; the other declaration forms give "Item-Id".
  Found by the parameter property test on the `test/property-based-params`
  branch. Not checked against the docs or for an origin.

### SSE generator is not closed when the client leaves mid-send

- Status: already reported upstream as discussion 16346 (2026-09-11). A
  team member said they will review it when they have time. Nothing to post.
- What happens: when the client disconnects while the endpoint generator is
  suspended at a `yield` (the producer task is blocked sending), nothing
  closes the generator. Its `finally` runs when the garbage collector
  finalizes it: after the yield dependencies have exited, in another task,
  and in one run 7 requests later.
- Counts, real uvicorn with uvloop and httptools, socket dropped after
  0.3 s, 20 drops each:
  - 0.162.0, items ready at once: cleanup before dependency exit 0, after
    15, not before server shutdown 5.
  - 0.162.5, same: 0, 18, 2.
  - Control, 50 ms between items (generator awaiting when cancelled): 20 of
    20 before dependency exit, on both versions.
- In-process harness: 14 of 20 fast-client cases and 8 of 16 stalled-client
  cases for SSE with an async generator; the same shape for generator
  endpoints with `response_class=StreamingResponse`.
- Origin: present in 0.135.0, the release that added SSE (PR 15030).
- Workaround, tested 20 of 20 on 0.162.0 and 0.162.5: a yield dependency
  that depends on the session, collects the stream generators and closes
  them in its exit code (`close_streams` in `app.py`).
- A returned `StreamingResponse(generator)` with a stalled client behaves
  the same way; that path is Starlette's, not FastAPI's.

### SSE keepalive task raises BrokenResourceError on disconnect

- Status: already reported upstream as discussion 15725 (2026-06-09). A
  team member said it is tracked internally. Nothing to post.
- What happens: the exit stack closes the output stream before it cancels
  the keepalive task. If that task is blocked sending, it raises
  `anyio.BrokenResourceError`, which leaves the app as an `ExceptionGroup`.
  uvicorn logs "Exception in ASGI application".
- Not in the upstream thread: every yield dependency sees that
  `ExceptionGroup` at its `yield`. A dependency that commits on success and
  rolls back on error rolls back, depending only on timing.
- Counts, same setup: 18 of 20 on 0.162.0 and 17 of 20 on 0.162.5 with items
  ready at once; 0 of 20 in the control.
- Origin: first seen in 0.135.1; 0 of 10 on 0.135.0.
- Workaround, tested: the same `close_streams` dependency drops
  `BrokenResourceError` from the group and re-raises anything else. 0 of 20
  errors and 20 of 20 clean dependency exits with it.
- The model's `design` variant (cancel the tasks before closing the output,
  close the generator explicitly) satisfies both invariants.

### Notes that are not findings

- `scope="function"` dependencies exit before a streaming body starts. The
  docs describe this as the purpose of the scope (advanced dependencies,
  early exit).
- With ASGI spec 2.4 the harness sees an `ExceptionGroup` leave the app on
  every SSE disconnect. uvicorn 0.54.0 reports spec 2.3, and this was not
  confirmed on a server that reports 2.4.
- The cancellation tests added in 0.162.5 fail 21 of 39 against 0.162.0.
  They cover synchronous yield dependencies only.

## Mutation score of upstream's tests (0.162.5, 2026-11-12)

`mutation/mutants.json` holds 25 hand-picked mistakes in the streaming code,
the request wrapper, dependency resolution and sync dependency cleanup. Each
was run against the full upstream suite (4,812 tests, `tests/property`
excluded). The suite caught 22 of 25. Full output is in
`mutation/results-0.162.5.txt`.

The three it missed:

- SSE: the producer tasks are not cancelled when the exit stack unwinds.
- SSE: no checkpoint after each event.
- Dependencies: an override loses the scope declared on the original
  `Depends`.

None of the three has been shown to change behaviour, so each may be an
equivalent mutant. The disconnect harness here does not tell the two SSE
ones apart from the baseline either; its streams are finite and short. Its
total violation count also varies between runs (175 to 179 of 288), while
the per-endpoint SSE rows are stable.

Both SSE survivors sit in the disconnect teardown path, the same path as the
two reported SSE problems above.

### Follow-up on the two SSE survivors (2026-11-12)

`stream-disconnect/endless.py` runs an endless SSE stream and disconnects
the client after 0, 1 or 3 events, 12 cases. On unmodified 0.162.5 the
request ended in 12 of 12 (two runs), and the generator's cleanup was late
in 12 of 12. With either SSE mutation applied the result was the same: the
request still ended in 12 of 12. So in the in-process harness neither
mutation changes behaviour. That was not checked on a real server with
socket backpressure.

Reactions added to discussions 16346 and 15725 on 2026-11-12. No comment
posted on either.

### Dependency graph test with overrides and scopes (2026-11-12)

`tests/property/test_dependency_graph.py` on the
`test/property-based-params` branch replaces the spike here. It adds
per-edge `scope`, dependency overrides and the startup scope rule to the
reference model.

- Passes on 0.162.0 and 0.162.5: 300 graphs in about 3 s. About 40% of
  cases run with overrides, 10% expect a startup error and 1% a request
  error.
- Of the 8 dependency mutations, it catches 7. The one it misses needs a
  validation error inside a sub-dependency, which the test does not
  generate; upstream's suite catches that one.
- It catches "an override loses the declared scope", which upstream's suite
  missed. Smallest case: a dependency declared with `scope="function"` is
  overridden by a generator that has its own function-scope sub-dependency.
  With the mutation the request fails with `DependencyScopeError`. So that
  mutation is a real gap in upstream's tests, not an equivalent mutant.
- No disagreement between FastAPI and the model was found.
