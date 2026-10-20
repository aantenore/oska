"""Diagnostic spans sent on to an OpenTelemetry collector, as well as to the file.

With `tracing.exporter: otlp` a deployment's spans reach whatever collector its
observability stack already reads, over OTLP. The file of `tracing.path` is
still written exactly as before: it is what the service-level report reads its
latencies from and what forgetting a session rewrites. The collector receives a
copy; the audit trail and the file stay the record.

A span keeps its name and its nesting, and its fields become attributes, but
only the fields named in `EXPORTED_FIELDS`: identifiers, kinds, counts and
flags. A field a span gains later is not exported until it is named there, so
what leaves the process is decided here and nowhere else, and it never includes
a payload, the person's text or the model's output. Each exported span also
carries the turn of the `turn` span it descends from, and the tenant and the
domain of the engine that recorded it where there is one. The session id is
exported only when `include_session` says so: a collector keeps what it is
sent, and forgetting a session cannot reach it there.

The OpenTelemetry SDK is the optional extra `otel`. It is imported only when a
deployment asks for this exporter, and a missing extra is a one-line error
naming it.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from oska.audit.trace import JsonlTraceSink, Span
from oska.config import OtlpConfig
from oska.storage.protocols import DEFAULT_NAMESPACE

__all__ = [
    "EXPORTED_FIELDS",
    "EXTRA",
    "INSTRUMENTATION",
    "OtlpTraceSink",
    "TraceExportError",
    "namespace_labels",
    "open_otlp_sink",
    "tracer_for",
]

EXTRA = "otel"
"""The optional extra that installs the OpenTelemetry SDK and its OTLP exporters."""

INSTRUMENTATION = "oska"
"""The instrumentation scope exported spans are recorded under."""

EXPORTED_FIELDS: Mapping[str, str] = {
    "session": "session.id",
    "turn": "oska.turn",
    "kind": "oska.kind",
    "source": "oska.source",
    "chosen": "oska.chosen",
    "tie": "oska.tie",
    "runbook": "oska.runbook",
    "guardrails": "oska.guardrails",
    "start": "oska.start",
    "stop": "oska.stop",
    "operation": "oska.operation",
    "approved": "oska.approved",
    "outcome": "oska.outcome",
}
"""Span fields that leave the process, and the attribute each becomes."""

_INHERITED = ("session", "turn")
"""Fields of the `turn` span every span nested in it carries as well."""

_LABELS = {"tenant": "oska.tenant", "domain": "oska.domain"}


class TraceExportError(RuntimeError):
    """The OTLP exporter cannot be built as configured."""


def namespace_labels(namespace: str) -> dict[str, str]:
    """The tenant and the domain a storage namespace names, or nothing for the default one.

    A pair's namespace is `tenant/domain`, possibly inside a scope; neither name
    can hold a slash, so the last two segments are always the pair.
    """
    if namespace == DEFAULT_NAMESPACE or "/" not in namespace:
        return {}
    tenant, domain = namespace.rsplit("/", 2)[-2:]
    return {"tenant": tenant, "domain": domain}


def _attribute(value: object) -> bool | int | float | str | None:
    if isinstance(value, bool | int | float | str):
        return value
    return None


def _exported(fields: Mapping[str, object]) -> dict[str, bool | int | float | str]:
    attributes: dict[str, bool | int | float | str] = {}
    for name, key in EXPORTED_FIELDS.items():
        value = _attribute(fields.get(name))
        if value is not None:
            attributes[key] = value
    return attributes


class OtlpTraceSink(JsonlTraceSink):
    """Writes the file as `JsonlTraceSink` does and hands each span to a tracer too.

    `tracer` is an OpenTelemetry tracer; `labels` names the tenant and the
    domain where the engine serves a pair. A `turn` span opened while the host
    application has a span of its own active is nested under that one.
    """

    def __init__(
        self,
        path: Path,
        tracer: Any,
        labels: Mapping[str, str] | None = None,
        *,
        include_session: bool = False,
    ) -> None:
        super().__init__(path)
        self._tracer = tracer
        self._include_session = include_session
        self._labels = {
            _LABELS[name]: value for name, value in (labels or {}).items() if name in _LABELS
        }
        self._live: dict[str, tuple[Any, dict[str, object]]] = {}

    def _opened(self, span: Span, parent: Span | None) -> None:
        from opentelemetry import trace

        context = None
        inherited: dict[str, object] = {}
        held = self._live.get(parent.span_id) if parent is not None else None
        if held is not None:
            context = trace.set_span_in_context(held[0])
            inherited = dict(held[1])
        for name in _INHERITED:
            if name in span.fields:
                inherited[name] = span.fields[name]
        exported = self._tracer.start_span(
            span.name,
            context=context,
            start_time=_nanoseconds(span),
        )
        self._live[span.span_id] = (exported, inherited)

    def _raised(self, span: Span, error: BaseException) -> None:
        from opentelemetry.trace import Status, StatusCode

        held = self._live.get(span.span_id)
        if held is not None:
            held[0].set_status(Status(StatusCode.ERROR, type(error).__name__))

    def _record(self, span: Span) -> None:
        super()._record(span)
        held = self._live.pop(span.span_id, None)
        if held is None:
            return
        exported, inherited = held
        attributes = {**self._labels, **_exported(inherited), **_exported(span.fields)}
        if not self._include_session:
            # A collector keeps what it receives, and forgetting a session cannot reach it.
            attributes.pop(EXPORTED_FIELDS["session"], None)
        exported.set_attributes(attributes)
        exported.end(end_time=_nanoseconds(span) + int(span.duration_ms * 1_000_000))


def _nanoseconds(span: Span) -> int:
    return int(span.started_at.timestamp() * 1_000_000) * 1_000


_TRACERS: dict[tuple[str | None, str, str, str | None], Any] = {}
_TRACERS_LOCK = threading.Lock()


def _missing(protocol: str) -> TraceExportError:
    return TraceExportError(
        f'tracing.exporter "otlp" with protocol {protocol} needs the OpenTelemetry SDK; add '
        f"the [{EXTRA}] extra to the requirement oska was installed with, as in: "
        f"pip install 'oska[{EXTRA}]'"
    )


def _headers(variable: str | None) -> dict[str, str] | None:
    """The headers `variable` holds as `key=value,key=value`, or None to leave them to the SDK."""
    if variable is None:
        return None
    raw = os.environ.get(variable, "")
    if not raw.strip():
        raise TraceExportError(
            f"tracing.otlp.headers_env names {variable}, which is not set in the environment"
        )
    headers: dict[str, str] = {}
    for entry in raw.split(","):
        if not entry.strip():
            continue
        key, separator, value = entry.partition("=")
        if not separator or not key.strip():
            raise TraceExportError(
                f"{variable} must hold headers as key=value pairs separated by commas"
            )
        headers[unquote(key.strip()).lower()] = unquote(value.strip())
    return headers


def tracer_for(config: OtlpConfig) -> Any:
    """A tracer exporting to the collector `config` names, one per distinct configuration.

    Every engine of a process configured alike shares one provider, one export
    queue and one background thread; the tenant and the domain travel on the
    spans, not on the provider.
    """
    key = (config.endpoint, config.protocol, config.service_name, config.headers_env)
    with _TRACERS_LOCK:
        held = _TRACERS.get(key)
        if held is not None:
            return held
        try:
            from opentelemetry.sdk.resources import SERVICE_NAME, Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            if config.protocol == "grpc":
                from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                    OTLPSpanExporter,
                )
            else:
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                    OTLPSpanExporter,
                )
        except ImportError as error:
            raise _missing(config.protocol) from error
        exporter = OTLPSpanExporter(endpoint=config.endpoint, headers=_headers(config.headers_env))
        provider = TracerProvider(resource=Resource.create({SERVICE_NAME: config.service_name}))
        provider.add_span_processor(BatchSpanProcessor(exporter))
        tracer = provider.get_tracer(INSTRUMENTATION)
        _TRACERS[key] = tracer
        return tracer


def open_otlp_sink(
    path: Path, config: OtlpConfig, namespace: str = DEFAULT_NAMESPACE
) -> OtlpTraceSink:
    """The sink a deployment with `tracing.exporter: otlp` records through."""
    return OtlpTraceSink(
        path,
        tracer_for(config),
        namespace_labels(namespace),
        include_session=config.include_session,
    )
