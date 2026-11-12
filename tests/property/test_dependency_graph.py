"""Property test: generated dependency graphs behave like a small reference model.

Each example builds an app whose endpoint depends on a random graph of sync,
async and generator dependencies, with `use_cache` and `scope` chosen per edge
and some dependencies overridden. The model below predicts which dependencies
run, in which order, and in which order generators are torn down.

Needs `hypothesis`, which is not a project dependency; skipped without it.
"""

import inspect
from dataclasses import dataclass
from typing import Any, Literal

from fastapi import Depends, FastAPI
from fastapi.exceptions import DependencyScopeError
from fastapi.testclient import TestClient
from hypothesis import event, given
from hypothesis import strategies as st

KINDS = ["sync", "async", "gen", "asyncgen"]
SCOPES: list[Literal["function", "request"] | None] = [None, "function", "request"]


@dataclass(frozen=True)
class Edge:
    target: int
    use_cache: bool
    scope: Literal["function", "request"] | None


@dataclass(frozen=True)
class Node:
    kind: str
    edges: tuple[Edge, ...]

    @property
    def is_generator(self) -> bool:
        return self.kind in ("gen", "asyncgen")


@dataclass(frozen=True)
class Graph:
    nodes: tuple[Node, ...]
    roots: tuple[Edge, ...]
    overrides: dict[int, Node]
    endpoint_fails: bool


def edges(draw: st.DrawFn, below: int, *, min_size: int = 0) -> tuple[Edge, ...]:
    if below == 0:
        return ()
    edge = st.builds(
        Edge,
        target=st.integers(0, below - 1),
        use_cache=st.booleans(),
        scope=st.sampled_from(SCOPES),
    )
    return tuple(draw(st.lists(edge, min_size=min_size, max_size=3)))


@st.composite
def graphs(draw: st.DrawFn) -> Graph:
    size = draw(st.integers(1, 5))
    # Edges only point at earlier nodes, so the graph has no cycles.
    nodes = tuple(
        Node(kind=draw(st.sampled_from(KINDS)), edges=edges(draw, i))
        for i in range(size)
    )
    overridden = draw(st.sets(st.integers(0, size - 1), max_size=2))
    overrides = {
        i: Node(kind=draw(st.sampled_from(KINDS)), edges=edges(draw, i))
        for i in sorted(overridden)
    }
    return Graph(
        nodes=nodes,
        roots=edges(draw, size, min_size=1),
        overrides=overrides,
        endpoint_fails=draw(st.booleans()),
    )


# --- reference model ---------------------------------------------------------


class ScopeRuleBroken(Exception):
    pass


def check_scope_rule(graph: Graph, node: Node, scope: str | None) -> None:
    """A request-scope generator may not directly depend on a function-scope
    dependency. Checked for a dependency and everything declared below it."""
    if node.is_generator and (scope or "request") == "request":
        if any(edge.scope == "function" for edge in node.edges):
            raise ScopeRuleBroken
    for edge in node.edges:
        check_scope_rule(graph, graph.nodes[edge.target], edge.scope)


@dataclass
class Expected:
    startup_error: bool = False
    request_error: bool = False
    calls: tuple[str, ...] = ()
    exits: tuple[str, ...] = ()


def predict(graph: Graph) -> Expected:
    try:
        for root in graph.roots:
            check_scope_rule(graph, graph.nodes[root.target], root.scope)
    except ScopeRuleBroken:
        return Expected(startup_error=True)

    calls: list[str] = []
    function_stack: list[str] = []
    request_stack: list[str] = []
    cache: set[tuple[int, str]] = set()

    def solve(edge: Edge) -> None:
        original = graph.nodes[edge.target]
        node, name = original, f"dep{edge.target}"
        if edge.target in graph.overrides:
            node, name = graph.overrides[edge.target], f"override{edge.target}"
        if graph.overrides:
            # With overrides present the dependency is analysed again per request.
            check_scope_rule(graph, node, edge.scope)
        for sub in node.edges:
            solve(sub)
        # The cache key comes from the original declaration, not the override.
        key = (
            edge.target,
            edge.scope or ("request" if original.is_generator else ""),
        )
        if edge.use_cache and key in cache:
            return
        calls.append(name)
        if node.is_generator:
            stack = function_stack if edge.scope == "function" else request_stack
            stack.append(name)
        cache.add(key)

    request_error = False
    try:
        for root in graph.roots:
            solve(root)
    except ScopeRuleBroken:
        request_error = True
    return Expected(
        request_error=request_error,
        calls=tuple(calls),
        exits=tuple(function_stack[::-1] + request_stack[::-1]),
    )


# --- the real app ------------------------------------------------------------


def make_callable(name: str, node: Node, funcs: list[Any], log: list[str]) -> Any:
    if node.kind == "sync":

        def call(**kwargs: Any) -> str:
            log.append(f"call {name}")
            return name

    elif node.kind == "async":

        async def call(**kwargs: Any) -> str:  # type: ignore[misc]
            log.append(f"call {name}")
            return name

    elif node.kind == "gen":

        def call(**kwargs: Any) -> Any:  # type: ignore[misc]
            log.append(f"call {name}")
            try:
                yield name
            finally:
                log.append(f"exit {name}")

    else:

        async def call(**kwargs: Any) -> Any:  # type: ignore[misc]
            log.append(f"call {name}")
            try:
                yield name
            finally:
                log.append(f"exit {name}")

    call.__name__ = name
    return with_signature(call, node.edges, funcs)


def with_signature(func: Any, edges: tuple[Edge, ...], funcs: list[Any]) -> Any:
    func.__signature__ = inspect.Signature(
        [
            inspect.Parameter(
                f"p{position}",
                inspect.Parameter.KEYWORD_ONLY,
                default=Depends(
                    funcs[edge.target], use_cache=edge.use_cache, scope=edge.scope
                ),
            )
            for position, edge in enumerate(edges)
        ]
    )
    return func


def build(graph: Graph, log: list[str]) -> FastAPI:
    funcs: list[Any] = []
    for index, node in enumerate(graph.nodes):
        funcs.append(make_callable(f"dep{index}", node, funcs, log))

    async def endpoint(**kwargs: Any) -> dict[str, bool]:
        log.append("endpoint")
        if graph.endpoint_fails:
            raise RuntimeError("endpoint failed")
        return {"ok": True}

    with_signature(endpoint, graph.roots, funcs)
    app = FastAPI()
    for index, node in graph.overrides.items():
        app.dependency_overrides[funcs[index]] = make_callable(
            f"override{index}", node, funcs, log
        )
    app.get("/")(endpoint)
    return app


@given(graph=graphs())
def test_dependency_graph_matches_model(graph: Graph) -> None:
    expected = predict(graph)
    event(
        "startup error"
        if expected.startup_error
        else "request error"
        if expected.request_error
        else "runs, with overrides"
        if graph.overrides
        else "runs, no overrides"
    )
    log: list[str] = []
    try:
        app = build(graph, log)
    except DependencyScopeError:
        assert expected.startup_error
        return
    assert not expected.startup_error

    response = TestClient(app, raise_server_exceptions=False).get("/")

    failed = expected.request_error or graph.endpoint_fails
    assert response.status_code == (500 if failed else 200)
    calls = tuple(e.removeprefix("call ") for e in log if e.startswith("call "))
    exits = tuple(e.removeprefix("exit ") for e in log if e.startswith("exit "))
    assert calls == expected.calls
    assert exits == expected.exits
    if expected.request_error:
        assert "endpoint" not in log
    else:
        # Every dependency is set up before the endpoint, and none is torn
        # down before it.
        assert log.index("endpoint") == len(calls)
