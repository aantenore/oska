"""The connector interface, its error type and the latency helper.

A connector binds the operations a catalog declares to something that can run
them. It knows nothing about the operations themselves: names and payloads are
handed to it, and it answers with a `ConnectorResult`.
"""

from __future__ import annotations

import functools
import time
from collections.abc import Callable
from typing import ParamSpec, Protocol, runtime_checkable

from opsdeck.contracts.common import ConnectorResult

__all__ = ["Connector", "ConnectorError", "timed"]

P = ParamSpec("P")


class ConnectorError(Exception):
    """A binding is unusable, or the call could not be made as declared."""


@runtime_checkable
class Connector(Protocol):
    """Runs one declared operation against one system."""

    id: str

    def execute(self, operation: str, payload: dict) -> ConnectorResult:
        """Run `operation` with `payload` and report what happened."""
        ...


def timed(fn: Callable[P, ConnectorResult]) -> Callable[P, ConnectorResult]:
    """Wrap an `execute` implementation so its result carries the elapsed time."""

    @functools.wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> ConnectorResult:
        started = time.perf_counter()
        result = fn(*args, **kwargs)
        elapsed = (time.perf_counter() - started) * 1000
        return result.model_copy(update={"latency_ms": int(round(elapsed))})

    return wrapper
