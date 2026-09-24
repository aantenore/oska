"""One call to a connector, as the turn records it.

A connector failure is an outcome and not a crash, so every call returns a
result. A call that did not reach the system at all is reported as what it is:
one capability that is temporarily unavailable, and the moment it may be tried
again.
"""

from __future__ import annotations

from oska.connectors.base import (
    Connector,
    ConnectorError,
    health_of,
    invoke,
    is_unavailable,
)
from oska.connectors.registry import ConnectorRegistry
from oska.contracts.common import ConnectorResult
from oska.engine.context import TurnContext

__all__ = ["call", "connector_for"]


def call(
    connectors: ConnectorRegistry,
    ctx: TurnContext,
    integration_id: str,
    operation: str,
    payload: dict,
    write: bool = False,
    idempotency_key: str | None = None,
) -> ConnectorResult:
    """Invoke one operation through the connector bound to its integration."""
    connector = connector_for(connectors, integration_id)
    if connector is None:
        result = ConnectorResult(
            ok=False, error=f"no connector is bound to {integration_id}"
        )
    else:
        try:
            result = invoke(
                connector,
                operation,
                payload,
                write=write,
                idempotency_key=idempotency_key,
            )
        except Exception as exc:  # a connector failure is an outcome, not a crash
            result = ConnectorResult(ok=False, error=f"{type(exc).__name__}: {exc}")
    ctx.emit(
        "execute",
        {
            "integration_id": integration_id,
            "operation": operation,
            "write": write,
            "stage": "call",
            "ok": result.ok,
            "error": result.error,
            "latency_ms": result.latency_ms,
        },
    )
    if is_unavailable(result):
        health = health_of(result)
        ctx.emit(
            "unavailable",
            {
                "integration_id": integration_id,
                "operation": operation,
                "write": write,
                "error": result.error,
                "breaker": health.state,
                "consecutive_failures": health.consecutive_failures,
                "retry_in_seconds": health.retry_in_seconds,
            },
        )
    return result


def connector_for(connectors: ConnectorRegistry, integration_id: str) -> Connector | None:
    """The connector bound to an integration, or nothing when none is."""
    try:
        return connectors.get(integration_id)
    except ConnectorError:
        return None
