"""`GET /metrics`: the service report a monitoring system scrapes.

`MetricsConfig.enabled` gates the route: a deployment that turns metrics off
answers 404 here, the same as a route nobody ever mounted, rather than an empty
or zeroed body that would read as a healthy scrape.

A report names every tenant a platform serves and how much each one does, so
where `IdentityConfig.required` is on it is read like the proposals are: by a
principal carrying the reviewer role, and by nobody without an identity.
`MetricsConfig.public` opens it to any caller anyway, for a scraper that
reaches the runtime over a private network and presents no identity. Where no
identity is required the route stays open, as every other read there is.

An application serving one catalog reports one series per metric, unlabeled.
An application serving a platform reports every (tenant, domain) the platform
declares, each computed from that pair's own audit trail and traces and
labeled with its `tenant` and its `domain`, so no pair is left out and no two
are summed into one number. A pair whose tenant turns metrics off is left out.

The trail is read in full to compute a report, so a report is kept for a short
interval and served again until it is over. The interval is
`MetricsConfig.cache_seconds` of the configuration this route reads, the
platform's own when it serves a platform. That configuration is read once per
application and kept. The cache is held on the application
as `app.state.metrics_cache`, built on the first scrape with that interval; an
application that sets one itself is answered from the one it set.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

from oska.api.errors import ApiError
from oska.api.identity import (
    application_runtime,
    is_reviewer,
    refuse_review,
    resolve_principal,
)
from oska.config import RuntimeConfig, Settings, state_path
from oska.observability.exposition import render_series
from oska.observability.metrics import ReportCache, ServiceReport

__all__ = ["CONTENT_TYPE", "metrics_router"]

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

metrics_router = APIRouter()


@metrics_router.get("/metrics")
def get_metrics(request: Request) -> PlainTextResponse:
    """The service-level numbers this deployment can report, as Prometheus text."""
    platform = getattr(request.app.state, "platform", None)
    held = application_runtime(request)
    _admitted(request, held)
    series: list[tuple[dict[str, str], ServiceReport]] = []
    if platform is None:
        settings: Settings = request.app.state.services.settings
        runtime = held
        if not runtime.metrics.enabled:
            raise _disabled()
        cache = _cache(request, runtime)
        series.append(({}, _report(cache, settings, runtime)))
    else:
        if not platform.runtime.metrics.enabled:
            raise _disabled()
        cache = _cache(request, platform.runtime)
        for tenant, domain in platform.pairs():
            runtime = platform.runtime_for(tenant)
            if not runtime.metrics.enabled:
                continue
            settings = platform.settings_for(domain, tenant)
            labels = {"tenant": tenant, "domain": domain}
            series.append((labels, _report(cache, settings, runtime)))
    return PlainTextResponse(render_series(series), media_type=CONTENT_TYPE)


def _admitted(request: Request, runtime: RuntimeConfig) -> None:
    """Refuse a scrape the configuration does not open to this caller."""
    identity = runtime.identity
    if not identity.required or runtime.metrics.public:
        return
    principal = resolve_principal(request, identity, session_id=None)
    if not is_reviewer(principal, identity):
        raise refuse_review(principal, identity)


def _cache(request: Request, runtime: RuntimeConfig) -> ReportCache:
    """The application's report cache, built with the configured interval when it has none."""
    state = request.app.state
    held = getattr(state, "metrics_cache", None)
    if held is None:
        held = ReportCache(runtime.metrics.cache_seconds)
        state.metrics_cache = held
    return held


def _disabled() -> ApiError:
    return ApiError(404, "metrics_disabled", "metrics are switched off in this deployment")


def _report(cache: ReportCache, settings: Settings, runtime: RuntimeConfig) -> ServiceReport:
    return cache.report(
        _audit_path(settings, runtime), trace_path=_trace_path(settings, runtime)
    )


def _audit_path(settings: Settings, runtime: RuntimeConfig) -> Path:
    return state_path(Path(settings.state_dir), runtime.audit.path)


def _trace_path(settings: Settings, runtime: RuntimeConfig) -> Path | None:
    """Where this deployment records traces, when it records them at all."""
    if not runtime.tracing.enabled:
        return None
    return state_path(Path(settings.state_dir), runtime.tracing.path)
