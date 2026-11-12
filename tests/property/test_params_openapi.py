"""Property tests: a declared parameter, its OpenAPI entry and its runtime
behaviour must agree, however the parameter is declared.

Prototype. Needs `hypothesis`, which is not a project dependency.
"""

import inspect
import re
from dataclasses import dataclass, field
from typing import Annotated, Any

import pytest

hypothesis = pytest.importorskip("hypothesis")

from fastapi import (  # noqa: E402
    Cookie,
    Depends,
    FastAPI,
    Form,
    Header,
    Query,
)
from fastapi.testclient import TestClient  # noqa: E402
from hypothesis import HealthCheck, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402
from pydantic import AliasChoices, BaseModel, Field  # noqa: E402

LOCATIONS = {"query": Query, "header": Header, "cookie": Cookie, "form": Form}
NAMES = ["x", "item_id", "q", "user_token", "limit"]
ALIASES = ["alias", "item-id", "X-Token", "other_name"]
MISSING = object()


@dataclass
class Spec:
    location: str
    name: str
    base: str  # int | str | float | bool
    is_list: bool
    nullable: bool
    default: Any  # MISSING means required
    opts: dict[str, Any] = field(default_factory=dict)

    @property
    def annotation(self) -> Any:
        tp: Any = {"int": int, "str": str, "float": float, "bool": bool}[self.base]
        if self.is_list:
            tp = list[tp]
        if self.nullable:
            tp = tp | None
        return tp


@st.composite
def specs(draw: st.DrawFn) -> Spec:
    location = draw(st.sampled_from(sorted(LOCATIONS)))
    base = draw(st.sampled_from(["int", "str", "float", "bool"]))
    is_list = location != "cookie" and draw(st.booleans())
    nullable = draw(st.booleans())
    opts: dict[str, Any] = {}
    if draw(st.booleans()):
        opts["description"] = "A description"
    if draw(st.booleans()):
        opts["deprecated"] = True
    if location == "header" and draw(st.booleans()):
        opts["convert_underscores"] = False
    if is_list:
        if draw(st.booleans()):
            opts["min_length"] = draw(st.integers(0, 2))
        if draw(st.booleans()):
            opts["max_length"] = draw(st.integers(2, 3))
    else:
        if base == "float":
            if draw(st.booleans()):
                opts[draw(st.sampled_from(["ge", "gt"]))] = draw(st.integers(-5, 5)) / 2
            if draw(st.booleans()):
                opts[draw(st.sampled_from(["le", "lt"]))] = draw(st.integers(5, 15)) / 2
        if base == "int":
            if draw(st.booleans()):
                opts[draw(st.sampled_from(["ge", "gt"]))] = draw(st.integers(-5, 5))
            if draw(st.booleans()):
                opts[draw(st.sampled_from(["le", "lt"]))] = draw(st.integers(5, 15))
            if draw(st.booleans()):
                opts["multiple_of"] = draw(st.integers(2, 3))
        elif base == "str":
            if draw(st.booleans()):
                opts["min_length"] = draw(st.integers(0, 3))
            if draw(st.booleans()):
                opts["max_length"] = draw(st.integers(3, 6))
            if draw(st.booleans()):
                opts["pattern"] = "^[a-z]+$"
    alias_kind = draw(
        st.sampled_from(["none", "alias", "validation_alias", "both", "choices"])
    )
    if alias_kind in ("alias", "both"):
        opts["alias"] = draw(st.sampled_from(ALIASES))
    if alias_kind in ("validation_alias", "both"):
        opts["validation_alias"] = draw(st.sampled_from(ALIASES))
    if alias_kind == "choices":
        opts["validation_alias"] = AliasChoices(
            *draw(
                st.lists(st.sampled_from(ALIASES), min_size=1, max_size=2, unique=True)
            )
        )
        if draw(st.booleans()):
            opts["alias"] = draw(st.sampled_from(ALIASES))
    if nullable and draw(st.booleans()):
        default: Any = None
    elif draw(st.booleans()):
        default = MISSING
    else:
        default = draw(values(base, is_list))
    return Spec(
        location=location,
        name=draw(st.sampled_from(NAMES)),
        base=base,
        is_list=is_list,
        nullable=nullable,
        default=default,
        opts=opts,
    )


def values(base: str, is_list: bool) -> st.SearchStrategy[Any]:
    one: st.SearchStrategy[Any] = {
        "int": st.integers(-20, 20),
        "str": st.text("abcXYZ019", min_size=1, max_size=8),
        "float": st.integers(-20, 20).map(lambda i: i / 4),
        "bool": st.booleans(),
    }[base]
    return st.lists(one, min_size=1, max_size=3) if is_list else one


# --- four ways to declare the same parameter --------------------------------


def _endpoint(annotation: Any, default: Any, name: str, get: Any = None) -> Any:
    def endpoint(**kwargs: Any) -> Any:
        value = kwargs[name]
        return {"value": get(value) if get else value}

    endpoint.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
        [
            inspect.Parameter(
                name,
                inspect.Parameter.KEYWORD_ONLY,
                annotation=annotation,
                default=default,
            )
        ]
    )
    return endpoint


def form_annotated(s: Spec) -> Any:
    loc = LOCATIONS[s.location]
    default = inspect.Parameter.empty if s.default is MISSING else s.default
    return _endpoint(Annotated[s.annotation, loc(**s.opts)], default, s.name)


def form_default(s: Spec) -> Any:
    loc = LOCATIONS[s.location]
    args = () if s.default is MISSING else (s.default,)
    return _endpoint(s.annotation, loc(*args, **s.opts), s.name)


def form_field(s: Spec) -> Any:
    loc = LOCATIONS[s.location]
    default = inspect.Parameter.empty if s.default is MISSING else s.default
    return _endpoint(
        Annotated[s.annotation, Field(**field_opts(s)), loc(**loc_opts(s))],
        default,
        s.name,
    )


def field_opts(s: Spec) -> dict[str, Any]:
    return {k: v for k, v in s.opts.items() if k != "convert_underscores"}


def loc_opts(s: Spec) -> dict[str, Any]:
    return {k: v for k, v in s.opts.items() if k == "convert_underscores"}


def form_model(s: Spec) -> Any:
    loc = LOCATIONS[s.location]
    args = () if s.default is MISSING else (s.default,)
    model = type(
        "Params",
        (BaseModel,),
        {
            "__annotations__": {s.name: s.annotation},
            s.name: Field(*args, **field_opts(s)),
        },
    )
    return _endpoint(
        Annotated[model, loc(**loc_opts(s))],
        inspect.Parameter.empty,
        "params",
        get=lambda m: m.__dict__[s.name],  # getattr warns on deprecated fields
    )


def form_nested(s: Spec) -> Any:
    loc = LOCATIONS[s.location]
    default = inspect.Parameter.empty if s.default is MISSING else s.default
    inner = Annotated[s.annotation, Field(**field_opts(s))]
    return _endpoint(Annotated[inner, loc(**loc_opts(s))], default, s.name)


def form_depends(s: Spec) -> Any:
    dependency = form_annotated(s)

    def endpoint(dep: Any = Depends(dependency)) -> Any:
        return dep

    return endpoint


FORMS = {
    "nested": form_nested,
    "depends": form_depends,
    "annotated": form_annotated,
    "default": form_default,
    "field": form_field,
    "model": form_model,
}


# --- a small JSON Schema checker for the keywords these specs can produce ---


def schema_accepts(schema: dict[str, Any], value: Any) -> bool:
    known = {
        "anyOf", "type", "items", "minimum", "maximum", "exclusiveMinimum",
        "exclusiveMaximum", "multipleOf", "minLength", "maxLength", "pattern",
        "title", "default", "minItems", "maxItems", "description", "deprecated",
    }  # fmt: skip
    assert set(schema) <= known, f"unhandled schema keywords: {set(schema) - known}"
    if "anyOf" in schema:
        return any(schema_accepts(sub, value) for sub in schema["anyOf"])
    tp = schema.get("type")
    if tp == "null":
        return value is None
    if tp == "array":
        return (
            isinstance(value, list)
            and schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", 99)
            and all(schema_accepts(schema.get("items", {}), v) for v in value)
        )
    if tp == "boolean":
        return isinstance(value, bool)
    if tp == "integer":
        ok = isinstance(value, int) and not isinstance(value, bool)
    elif tp == "number":
        ok = isinstance(value, (int, float)) and not isinstance(value, bool)
    elif tp == "string":
        ok = isinstance(value, str)
    else:
        raise AssertionError(f"unhandled type {tp!r}")
    if not ok:
        return False
    checks = [
        ("minimum", lambda b: value >= b),
        ("maximum", lambda b: value <= b),
        ("exclusiveMinimum", lambda b: value > b),
        ("exclusiveMaximum", lambda b: value < b),
        ("multipleOf", lambda b: value % b == 0),
        ("minLength", lambda b: len(value) >= b),
        ("maxLength", lambda b: len(value) <= b),
        ("pattern", lambda b: re.search(b, value) is not None),
    ]
    return all(check(schema[key]) for key, check in checks if key in schema)


# --- driving the app ---------------------------------------------------------


class Raw(str):
    """Text sent as-is, with no claim about whether it is valid."""


RAW = st.sampled_from(
    ["", "abc", " 5", "5 ", "1.0", "1e1", "TRUE", "on", "null", "+3", "0x1"]
).map(Raw)


def wire(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def send(client: TestClient, location: str, name: str, value: Any) -> Any:
    if location == "form":
        items = (
            [] if value is MISSING else value if isinstance(value, list) else [value]
        )
        body = "&".join(f"{name}={wire(v)}" for v in items)
        return client.post(
            "/",
            content=body,
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
    if value is MISSING:
        return client.get("/")
    items = value if isinstance(value, list) else [value]
    if isinstance(value, Raw) and location == "header":
        value = Raw(value.strip())  # the client rejects padded header values
        items = [value]
    if location == "query":
        return client.get("/", params=[(name, wire(v)) for v in items])
    if location == "header":
        return client.get("/", headers=[(name, wire(v)) for v in items])
    return client.get("/", headers={"cookie": f"{name}={wire(items[0])}"})


def published(app: FastAPI, location: str) -> dict[str, Any]:
    if location == "form":
        return published_form(app)
    params = app.openapi()["paths"]["/"]["get"].get("parameters", [])
    matching = [p for p in params if p["in"] == location]
    assert len(matching) == 1, params
    # Lead 1 (cosmetic): header titles differ between forms ("Item-Id" vs "Item Id")
    matching[0]["schema"].pop("title", None)
    return matching[0]


def published_form(app: FastAPI) -> dict[str, Any]:
    doc = app.openapi()
    content = doc["paths"]["/"]["post"]["requestBody"]["content"]
    assert list(content) == ["application/x-www-form-urlencoded"], content
    schema = content["application/x-www-form-urlencoded"]["schema"]
    if "$ref" in schema:
        schema = doc["components"]["schemas"][schema["$ref"].rsplit("/", 1)[1]]
    assert len(schema["properties"]) == 1, schema
    ((name, prop),) = schema["properties"].items()
    prop.pop("title", None)
    return {
        "name": name,
        "in": "form",
        "required": name in schema.get("required", []),
        "schema": prop,
    }


def build(form: str, s: Spec) -> FastAPI:
    app = FastAPI()
    method = app.post if s.location == "form" else app.get
    method("/")(FORMS[form](s))
    return app


@settings(max_examples=300, deadline=None, suppress_health_check=list(HealthCheck))
@given(data=st.data(), s=specs())
def test_declaration_forms_agree(data: st.DataObject, s: Spec) -> None:
    apps = {form: build(form, s) for form in FORMS}
    docs = {form: published(app, s.location) for form, app in apps.items()}
    # 1. every declaration form publishes the same parameter
    for form, doc in docs.items():
        assert doc == docs["annotated"], (form, doc, docs["annotated"])

    doc = docs["annotated"]
    value = data.draw(
        st.one_of(st.just(MISSING), values(s.base, s.is_list), RAW), label="value"
    )
    names = {doc["name"], s.name, s.name.replace("_", "-")}
    for key in ("alias", "validation_alias"):
        alias = s.opts.get(key)
        names.update(alias.choices if isinstance(alias, AliasChoices) else [alias])
    names.discard(None)
    name = data.draw(st.sampled_from(sorted(names)), label="name")
    results = {}
    for form, app in apps.items():
        response = send(TestClient(app), s.location, name, value)
        results[form] = (response.status_code, response.json().get("value", "n/a"))
    # 2. every declaration form answers the request the same way
    for form, result in results.items():
        assert result == results["annotated"], (form, result, results["annotated"])

    status, echoed = results["annotated"]
    hypothesis.event(f"{s.location} sent={value is not MISSING} status={status}")
    if name != doc["name"] or isinstance(value, Raw):
        return
    if value is MISSING:
        # 3. `required` in the document matches what the app enforces
        assert (status == 422) == doc.get("required", False)
        if status == 200:
            assert echoed == (None if s.default is MISSING else s.default)
            if s.default is not MISSING and s.default is not None:
                assert doc["schema"].get("default") == s.default
    else:
        # 4. the published schema accepts exactly the values the app accepts
        assert (status == 200) == schema_accepts(doc["schema"], value)
        if status == 200:
            assert echoed == value
