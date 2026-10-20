"""The HTTP application: one runtime behind a small set of routes.

Every call is resolved to an identity before its handler runs, in one of two
modes `IdentityConfig.mode` chooses. In `proxy` mode an authenticated proxy in
front of the runtime sets the subject and attributes headers and the runtime
takes them as given: it verifies nothing there, so the guarantee that a write
is bound to the person who asked for it is exactly as strong as the
deployment's guarantee that nothing reaches this port except through that
proxy. In `token` mode the caller presents a signed session token and the
runtime verifies its signature, claims, validity window and session itself.
With `IdentityConfig.required`, on by default in `token` mode, a call without
an identity is refused; `/health` stays open, and `/metrics` is a reviewer's
read unless `MetricsConfig.public` opens it.

Reading proposals and change requests and moving a proposal is a reviewer's
work: a principal reaches those routes only when its `roles` attribute holds
`IdentityConfig.reviewer_role`. Drafting a proposal from a session is open to
the session's own principal and to a reviewer. A transition is recorded under
the caller's principal whatever its body says. A call without an identity
reaches the reviewer's routes only where `IdentityConfig.single_user` says it
is the deployment's one user; there it names its actor in `by`, and that name
is recorded as stated and not verified. Anywhere else it may converse and
nothing more.

The runtime configuration is read once, when the application is built, and
kept on it as `app.state.runtime`: the platform's own when it serves a
platform, else the file the settings name.

This process serves a catalog and does not write one. The catalog roots it
reads are read-only to it: the only thing publication produces is a change
request under the configured change request root, and installing one is a
command a person runs. No route here applies a change request, and the runtime
holds no function that would let one be applied without naming a catalog root.

Every handler works against the runtime `oska.services` assembles, the same
one the command line uses, so the process holds one engine, one catalog and one
proposal store for its lifetime. The handlers are synchronous, so the framework
runs them in a threadpool and two turns of that one engine overlap: anything
counted for the length of a turn is counted per turn and not on the engine, as
the model budget is.

Given a platform, the application serves every domain it declares, each for its
tenants: the platform hands out one service set per (tenant, domain) and the
routes choose among them per request. The default domain is built at start, so
a catalog that does not hold up stops the process before it listens. When the
application shuts down it closes the platform, and every runtime it holds, and
then the model provider it was handed, which releases its connections.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from oska.api.metrics import metrics_router
from oska.api.routes import ApiError, router
from oska.config import Settings, load_runtime_config
from oska.engine.engine import Engine
from oska.planner.provider import ModelProvider
from oska.platform import Platform
from oska.services import PROPOSALS_DIRNAME, Services, build_services
from oska.storage import ProposalRecordStore

__all__ = ["PROPOSALS_DIRNAME", "Services", "create_app"]

API_TITLE = "oska"


def create_app(
    settings: Settings | None = None,
    *,
    engine: Engine | None = None,
    proposal_store: ProposalRecordStore | None = None,
    platform: Platform | None = None,
    provider: ModelProvider | None = None,
) -> FastAPI:
    """Build the application. Anything not passed in is built from the settings.

    With a platform, the routes act on its domains and `engine` and
    `proposal_store` are not used. `provider` is the model provider the runtime
    reasons with; a platform was built with its own, and the one given here is
    the one it was built with. Either way the application closes it when it
    shuts down.
    """
    app = FastAPI(title=API_TITLE, lifespan=_lifespan)
    app.state.platform = platform
    app.state.provider = provider
    app.state.services = (
        platform.services()
        if platform is not None
        else build_services(
            settings, engine=engine, proposal_store=proposal_store, provider=provider
        )
    )
    app.state.runtime = (
        platform.runtime
        if platform is not None
        else load_runtime_config(settings=app.state.services.settings)
    )
    app.include_router(router)
    app.include_router(metrics_router)
    _register_errors(app)
    return app


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Serve until shutdown, then close the platform and the provider it was given, if any."""
    try:
        yield
    finally:
        platform: Platform | None = getattr(app.state, "platform", None)
        try:
            if platform is not None:
                platform.close()
        finally:
            close = getattr(getattr(app.state, "provider", None), "close", None)
            if callable(close):
                close()


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
