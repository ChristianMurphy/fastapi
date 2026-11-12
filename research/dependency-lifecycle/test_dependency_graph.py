"""Feasibility spike: generated dependency graphs checked against a tiny model."""

import inspect
from typing import Any

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

KINDS = ["sync", "async", "gen", "asyncgen"]


@st.composite
def graphs(draw):
    n = draw(st.integers(1, 6))
    nodes = []
    for i in range(n):
        kind = draw(st.sampled_from(KINDS))
        # edges only to earlier nodes: a DAG by construction
        edges = draw(
            st.lists(st.tuples(st.integers(0, i - 1), st.booleans()), max_size=3)
            if i
            else st.just([])
        )
        nodes.append((kind, edges))
    roots = draw(
        st.lists(
            st.tuples(st.integers(0, n - 1), st.booleans()), min_size=1, max_size=3
        )
    )
    fail = draw(st.booleans())
    return nodes, roots, fail


def build(nodes, roots, fail, log):
    funcs: list[Any] = []

    def params(edges):
        return [
            inspect.Parameter(
                f"p{k}",
                inspect.Parameter.KEYWORD_ONLY,
                default=Depends(funcs[j], use_cache=cache),
            )
            for k, (j, cache) in enumerate(edges)
        ]

    for i, (kind, edges) in enumerate(nodes):
        if kind == "sync":

            def f(_i=i, **kw):
                log.append(("call", _i))
                return _i
        elif kind == "async":

            async def f(_i=i, **kw):
                log.append(("call", _i))
                return _i
        elif kind == "gen":

            def f(_i=i, **kw):
                log.append(("call", _i))
                try:
                    yield _i
                finally:
                    log.append(("exit", _i))
        else:

            async def f(_i=i, **kw):
                log.append(("call", _i))
                try:
                    yield _i
                finally:
                    log.append(("exit", _i))

        f.__signature__ = inspect.Signature(params(edges))
        f.__name__ = f"dep{i}"
        funcs.append(f)

    async def endpoint(**kw):
        log.append(("endpoint", -1))
        if fail:
            raise RuntimeError("boom")
        return {"ok": True}

    endpoint.__signature__ = inspect.Signature(params(roots))
    app = FastAPI()
    app.get("/")(endpoint)
    return app


def model_calls(nodes, roots):
    """Reference model: depth-first, cached deps run once per request."""
    order, cache = [], set()

    def visit(j, use_cache):
        for k, c in nodes[j][1]:
            visit(k, c)
        if use_cache and j in cache:
            return
        order.append(j)
        cache.add(j)

    for j, c in roots:
        visit(j, c)
    return order


@settings(max_examples=300, deadline=None, suppress_health_check=list(HealthCheck))
@given(graphs())
def test_dependency_graph(g):
    nodes, roots, fail = g
    log: list = []
    app = build(nodes, roots, fail, log)
    with TestClient(app, raise_server_exceptions=False) as client:
        r = client.get("/")
    assert r.status_code == (500 if fail else 200)
    calls = [i for ev, i in log if ev == "call"]
    exits = [i for ev, i in log if ev == "exit"]
    # 1. call order and count match the reference model
    assert calls == model_calls(nodes, roots)
    # 2. every generator that was entered is exited exactly once, in reverse order
    entered = [i for i in calls if nodes[i][0] in ("gen", "asyncgen")]
    assert exits == entered[::-1]
    # 3. the endpoint runs after all setup and before any teardown
    evs = [ev for ev, _ in log]
    assert evs.index("endpoint") == len(calls)
