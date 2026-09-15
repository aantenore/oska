"""The HTTP application: one runtime behind a small set of routes.

There is no authentication here. The service assumes an authenticated proxy in
front of it, which decides who may reach it and which identity is on a call.
The `by` field of a proposal transition is recorded as it arrives, and so is the
identity a turn runs under; nothing in this package verifies that the caller is
who it says it is. The runtime trusts the proxy and checks nothing of its own,
which is worth stating plainly: the guarantee that a write is bound to the
person who asked for it is exactly as strong as the deployment's guarantee that
nothing reaches this port except through that proxy.

This process serves a catalog and does not write one. The catalog roots it
reads are read-only to it: the only thing publication produces is a change
request under the configured change request root, and installing one is a
command a person runs. No route here applies a change request, and the runtime
holds no function that would let one be applied without naming a catalog root.

Every handler works against the runtime `opsdeck.services` assembles, the same
one the command line uses, so the process holds one engine, one catalog and one
proposal store for its lifetime. The handlers are synchronous, so the framework
runs them in a threadpool and two turns of that one engine overlap: anything
counted for the length of a turn is counted per turn and not on the engine, as
the model budget is.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from opsdeck.api.routes import ApiError, router
from opsdeck.config import Settings
from opsdeck.engine.engine import Engine
from opsdeck.learning.proposals import ProposalStore
from opsdeck.services import PROPOSALS_DIRNAME, Services, build_services

__all__ = ["PROPOSALS_DIRNAME", "Services", "create_app"]

API_TITLE = "opsdeck"


def create_app(
    settings: Settings | None = None,
    *,
    engine: Engine | None = None,
    proposal_store: ProposalStore | None = None,
) -> FastAPI:
    """Build the application. Anything not passed in is built from the settings."""
    app = FastAPI(title=API_TITLE)
    app.state.services = build_services(
        settings, engine=engine, proposal_store=proposal_store
    )
    app.include_router(router)
    _register_errors(app)
    return app


def _register_errors(app: FastAPI) -> None:
    """Every error leaves as `{error, detail}`."""

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code, content={"error": exc.error, "detail": exc.detail}
        )

    @app.exception_handler(RequestValidationError)
    async def _invalid_request(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={"error": "invalid_request", "detail": _first_message(exc)},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": "http_error", "detail": str(exc.detail)},
        )


def _first_message(exc: RequestValidationError) -> str:
    for error in exc.errors():
        location = ".".join(str(part) for part in error.get("loc", ()))
        return f"{location}: {error.get('msg', 'invalid value')}".strip(": ")
    return "the request body could not be read"
