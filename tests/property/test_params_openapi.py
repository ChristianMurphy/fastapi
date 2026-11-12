"""Property tests: a declared parameter, its OpenAPI entry and its runtime
behaviour must agree, however the parameter is declared.

Needs `hypothesis`, which is not a project dependency; skipped without it.
"""

import inspect
import re
from dataclasses import dataclass, field
from typing import Annotated, Any

from fastapi import (
    Cookie,
    Depends,
    FastAPI,
    Form,
    Header,
    Query,
)
from fastapi.testclient import TestClient
from hypothesis import event, given
from hypothesis import strategies as st
from pydantic import AliasChoices, BaseModel, Field

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


def optional(**strategies: st.SearchStrategy[Any]) -> st.SearchStrategy[dict[str, Any]]:
    """A dict holding any subset of the given options."""
    return st.fixed_dictionaries({}, optional=strategies)


def bounds(
    low: st.SearchStrategy[Any], high: st.SearchStrategy[Any]
) -> st.SearchStrategy[dict[str, Any]]:
    """At most one lower bound (ge or gt) and one upper bound (le or lt)."""
    lower = st.dictionaries(st.sampled_from(["ge", "gt"]), low, max_size=1)
    upper = st.dictionaries(st.sampled_from(["le", "lt"]), high, max_size=1)
    return st.builds(lambda lo, hi: {**lo, **hi}, lower, upper)


def halves(low: int, high: int) -> st.SearchStrategy[float]:
    return st.integers(low, high).map(lambda i: i / 2)


CONSTRAINTS: dict[str, st.SearchStrategy[dict[str, Any]]] = {
    "int": st.builds(
        lambda a, b: {**a, **b},
        bounds(st.integers(-5, 5), st.integers(5, 15)),
        optional(multiple_of=st.integers(2, 3)),
    ),
    "float": bounds(halves(-5, 5), halves(5, 15)),
    "str": optional(
        min_length=st.integers(0, 3),
        max_length=st.integers(3, 6),
        pattern=st.just("^[a-z]+$"),
    ),
    "bool": st.just({}),
    "list": optional(min_length=st.integers(0, 2), max_length=st.integers(2, 3)),
}
ALIAS_CHOICES = st.lists(
    st.sampled_from(ALIASES), min_size=1, max_size=2, unique=True
).map(lambda names: AliasChoices(*names))
NAMING = optional(
    alias=st.sampled_from(ALIASES),
    validation_alias=st.one_of(st.sampled_from(ALIASES), ALIAS_CHOICES),
    description=st.just("A description"),
    deprecated=st.just(True),
)


@st.composite
def specs(draw: st.DrawFn) -> Spec:
    location = draw(st.sampled_from(sorted(LOCATIONS)))
    base = draw(st.sampled_from(["int", "str", "float", "bool"]))
    is_list = location != "cookie" and draw(st.booleans())
    nullable = draw(st.booleans())
    header_only = optional(convert_underscores=st.just(False))
    opts = {
        **draw(CONSTRAINTS["list" if is_list else base]),
        **draw(NAMING),
        **draw(header_only if location == "header" else st.just({})),
    }
    defaults = [st.just(MISSING), values(base, is_list)]
    default = draw(st.one_of(*defaults, *([st.none()] if nullable else [])))
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


def with_signature(func: Any, parameters: list[inspect.Parameter]) -> Any:
    func.__signature__ = inspect.Signature(parameters)
    return func


def _endpoint(annotation: Any, default: Any, name: str, get: Any = None) -> Any:
    def endpoint(**kwargs: Any) -> Any:
        value = kwargs[name]
        return {"value": get(value) if get else value}

    return with_signature(
        endpoint,
        [
            inspect.Parameter(
                name,
                inspect.Parameter.KEYWORD_ONLY,
                annotation=annotation,
                default=default,
            )
        ],
    )


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


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


TYPE_CHECKS: dict[str, Any] = {
    "null": lambda v: v is None,
    "boolean": lambda v: isinstance(v, bool),
    "integer": lambda v: is_number(v) and isinstance(v, int),
    "number": is_number,
    "string": lambda v: isinstance(v, str),
    "array": lambda v: isinstance(v, list),
}
KEYWORD_CHECKS: dict[str, Any] = {
    "items": lambda sub, v: all(schema_accepts(sub, item) for item in v),
    "minItems": lambda bound, v: len(v) >= bound,
    "maxItems": lambda bound, v: len(v) <= bound,
    "minimum": lambda bound, v: v >= bound,
    "maximum": lambda bound, v: v <= bound,
    "exclusiveMinimum": lambda bound, v: v > bound,
    "exclusiveMaximum": lambda bound, v: v < bound,
    "multipleOf": lambda bound, v: v % bound == 0,
    "minLength": lambda bound, v: len(v) >= bound,
    "maxLength": lambda bound, v: len(v) <= bound,
    "pattern": lambda regex, v: re.search(regex, v) is not None,
}
ANNOTATIONS = {"anyOf", "type", "title", "default", "description", "deprecated"}


def schema_accepts(schema: dict[str, Any], value: Any) -> bool:
    unhandled = set(schema) - ANNOTATIONS - set(KEYWORD_CHECKS)
    assert not unhandled, f"unhandled schema keywords: {unhandled}"
    if "anyOf" in schema:
        return any(schema_accepts(sub, value) for sub in schema["anyOf"])
    return TYPE_CHECKS[schema["type"]](value) and all(
        check(schema[keyword], value)
        for keyword, check in KEYWORD_CHECKS.items()
        if keyword in schema
    )


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
    if location == "query":
        return client.get("/", params=[(name, wire(v)) for v in items])
    if location == "header":
        # The client rejects padded header values, so strip them.
        return client.get("/", headers=[(name, wire(v).strip()) for v in items])
    return client.get("/", headers={"cookie": f"{name}={wire(items[0])}"})


def published(app: FastAPI, location: str) -> dict[str, Any]:
    if location == "form":
        return published_form(app)
    params = app.openapi()["paths"]["/"]["get"].get("parameters", [])
    matching = [p for p in params if p["in"] == location]
    assert len(matching) == 1, params
    # Titles differ between declaration forms for headers ("Item-Id", "Item Id").
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
    names: set[str] = {doc["name"], s.name, s.name.replace("_", "-")}
    for alias in (s.opts.get("alias"), s.opts.get("validation_alias")):
        choices = alias.choices if isinstance(alias, AliasChoices) else [alias]
        names.update(choice for choice in choices if isinstance(choice, str))
    name = data.draw(st.sampled_from(sorted(names)), label="name")
    results = {}
    for form, app in apps.items():
        response = send(TestClient(app), s.location, name, value)
        results[form] = (response.status_code, response.json().get("value", "n/a"))
    # 2. every declaration form answers the request the same way
    for form, result in results.items():
        assert result == results["annotated"], (form, result, results["annotated"])

    status, echoed = results["annotated"]
    event(f"{s.location} sent={value is not MISSING} status={status}")
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
