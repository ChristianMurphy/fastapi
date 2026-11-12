# Dependency lifecycle: model, spike and tracker

Working notes, not part of FastAPI and not proposed upstream. Nothing here
has been posted anywhere.

## Files

- `deps.qnt`: a Quint model of one request with three chained dependencies,
  each a generator or a plain callable, each edge with a declared scope, and
  cancellation after the endpoint or after the response.
- `scope_transitive.py`: the confirm script for the one violation the model
  produced.
- `test_dependency_graph.py`: a Hypothesis test that generates dependency
  graphs (sync, async, generator, async generator, `use_cache` on or off) and
  compares call count, call order and teardown order with a small reference
  model.

## Running

```
npm i -g @informalsystems/quint
quint run deps.qnt --backend=typescript --invariant=exactlyOnce --max-samples=5000 --max-steps=8
quint run deps.qnt --backend=typescript --invariant=exitBeforeSubs --max-samples=5000 --max-steps=8 --seed=1
python scope_transitive.py
pytest -o addopts="" test_dependency_graph.py   # needs hypothesis
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
