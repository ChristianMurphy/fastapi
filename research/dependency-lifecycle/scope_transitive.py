"""A request-scope dependency reaches a function-scope one through a plain
(non-yield) dependency. The direct form is rejected at startup; this is not."""

from typing import Annotated

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

log: list[str] = []


class Session:
    open = True


def session():  # used with scope="function"
    s = Session()
    yield s
    s.open = False
    log.append("session closed")


def repo(s: Annotated[Session, Depends(session, scope="function")]):  # plain, no yield
    return s


def unit_of_work(r: Annotated[Session, Depends(repo)]):  # default scope: request
    yield r
    log.append(f"unit_of_work exit: session.open={r.open}")


def build(direct: bool) -> FastAPI:
    app = FastAPI()
    if direct:

        def uow_direct(s: Annotated[Session, Depends(session, scope="function")]):
            yield s

        dep = uow_direct
    else:
        dep = unit_of_work

    @app.get("/")
    def endpoint(u: Annotated[Session, Depends(dep)]):
        log.append("endpoint")
        return {}

    return app


for direct in (True, False):
    log.clear()
    try:
        app = build(direct)
        r = TestClient(app).get("/")
        print(f"direct={direct}: status {r.status_code}, log={log}")
    except Exception as e:
        print(
            f"direct={direct}: rejected at startup: {type(e).__name__}: {str(e)[:110]}"
        )
