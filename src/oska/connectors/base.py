"""The connector interface, its result, its breaker and the latency helper.

A connector binds the operations a catalog declares to something that can run
them. It knows nothing about the operations themselves: names and payloads are
handed to it, and it answers with a `ConnectorOutcome`.

Availability is part of that answer. "I cannot do that" and "I usually can, but
not right now" are different statements, and the connector is the only place
that knows which one it is making. Every result therefore says what kind of
failure it was:

`transport`  the call did not reach the system, or the system did not complete
             it: a timeout, a refused connection, a host that is not there, a
             server reporting that it is broken, or a breaker that is open.
             Retryable, and the only kind that counts towards the breaker.
`refused`    the system answered and the answer was no: a record that does not
             exist, a value it would not take, a permission denied. Terminal,
             and never an outage — a record that is missing is not a system
             that is down.
`rejected`   the request was never sent, because it was not well formed: an
             operation the binding does not declare, a payload missing a field
             the call needs. Terminal, and it counts for nothing, because it
             says nothing about the system at all.

The breaker follows from that distinction. After `failures_before_open`
consecutive transport failures a connector stops being called: calls are
refused here, without reaching the system, for `recovery_seconds`. One attempt
is then let through, and it either closes the breaker or opens it again. A
connector whose breaker is open degrades the operations of that one binding;
nothing else in a conversation changes.
"""

from __future__ import annotations

import functools
import math
import time
from collections.abc import Callable
from typing import Literal, ParamSpec, Protocol, runtime_checkable

from pydantic import BaseModel

from oska.config import ResilienceConfig
from oska.contracts.common import ConnectorResult
from oska.contracts.receipt import Classification

__all__ = [
    "BaseConnector",
    "BreakerState",
    "CircuitBreaker",
    "Connector",
    "ConnectorError",
    "ConnectorHealth",
    "ConnectorOutcome",
    "FailureKind",
    "as_outcome",
    "classification_of",
    "health_of",
    "is_unavailable",
    "refused",
    "rejected",
    "succeeded",
    "timed",
    "transport_failure",
]

P = ParamSpec("P")

BreakerState = Literal["closed", "open", "probing"]
"""`closed` while calls go out, `open` while they are refused, `probing` for the one attempt."""

FailureKind = Literal["transport", "refused", "rejected"]
"""What kind of failure a result reports: see the module docstring."""


class ConnectorError(Exception):
    """A binding is unusable, or the call could not be made as declared."""


class ConnectorHealth(BaseModel):
    """How a connector is faring, as small as it can be said.

    `retry_in_seconds` is how long the breaker still refuses calls, rounded up
    to the whole second, and nothing at all while calls are going out.
    """

    state: BreakerState = "closed"
    consecutive_failures: int = 0
    retry_in_seconds: int | None = None


class ConnectorOutcome(ConnectorResult):
    """What a connector returned, and whether another attempt is sensible.

    `failure` says what kind of failure it was and `classification` what may be
    done about it; both are empty when the call succeeded. `health` carries the
    state of the breaker at the moment the call ended.
    """

    failure: FailureKind | None = None
    classification: Classification | None = None
    health: ConnectorHealth | None = None


@runtime_checkable
class Connector(Protocol):
    """Runs one declared operation against one system."""

    id: str

    @property
    def health(self) -> ConnectorHealth:
        """Whether this connector is being called at all, and when it may next be tried."""
        ...

    def execute(self, operation: str, payload: dict) -> ConnectorOutcome:
        """Run `operation` with `payload` and report what happened."""
        ...


def succeeded(data: dict | None = None) -> ConnectorOutcome:
    """The system answered, and the answer is the data."""
    return ConnectorOutcome(ok=True, data=dict(data or {}))


def transport_failure(error: str) -> ConnectorOutcome:
    """The call did not reach the system, or the system did not complete it."""
    return ConnectorOutcome(ok=False, error=error, failure="transport", classification="retryable")


def refused(error: str) -> ConnectorOutcome:
    """The system answered, and the answer was no."""
    return ConnectorOutcome(ok=False, error=error, failure="refused", classification="terminal")


def rejected(error: str) -> ConnectorOutcome:
    """The request was never sent, because it was not well formed."""
    return ConnectorOutcome(ok=False, error=error, failure="rejected", classification="terminal")


def as_outcome(result: ConnectorResult) -> ConnectorOutcome:
    """Any connector result as an outcome, classifying one that does not say.

    A failure that carries no classification is taken to be terminal: a result
    of unknown provenance is never repeated on this system's initiative.
    """
    if isinstance(result, ConnectorOutcome):
        return result
    if result.ok:
        return ConnectorOutcome(ok=True, data=result.data, latency_ms=result.latency_ms)
    return ConnectorOutcome(
        ok=False,
        data=result.data,
        error=result.error,
        latency_ms=result.latency_ms,
        failure="refused",
        classification="terminal",
    )


def is_unavailable(result: ConnectorResult) -> bool:
    """Whether this result is a capability that is down rather than one that said no."""
    return not result.ok and getattr(result, "classification", None) == "retryable"


def classification_of(result: ConnectorResult) -> Classification | None:
    """How this result says its failure should be treated, when it says."""
    classification = getattr(result, "classification", None)
    return classification if classification in ("retryable", "terminal") else None


def health_of(result: ConnectorResult) -> ConnectorHealth:
    """The health a result carries, or an unremarkable one when it carries none."""
    health = getattr(result, "health", None)
    return health if isinstance(health, ConnectorHealth) else ConnectorHealth()


class CircuitBreaker:
    """Stops calling a system that keeps failing, and finds out when it is back.

    Only transport failures count. A system that answered — with a record, or
    with a refusal such as a not-found — is a system that is there, and closes
    the breaker again. A request that was never sent counts for nothing.
    """

    def __init__(
        self,
        failures_before_open: int = 3,
        recovery_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._threshold = max(1, int(failures_before_open))
        self._recovery = max(0.0, float(recovery_seconds))
        self._clock = clock
        self._state: BreakerState = "closed"
        self._failures = 0
        self._opened_at = 0.0

    @property
    def state(self) -> BreakerState:
        """What the breaker is doing right now."""
        return self._state

    def health(self) -> ConnectorHealth:
        """The state, the run of failures behind it, and when a call may go out again."""
        return ConnectorHealth(
            state=self._state,
            consecutive_failures=self._failures,
            retry_in_seconds=self._remaining() if self._state == "open" else None,
        )

    def reset(self) -> None:
        """Forget the run of failures and let calls out again."""
        self._state = "closed"
        self._failures = 0
        self._opened_at = 0.0

    def allows(self) -> bool:
        """Whether a call may go out, letting one through once the recovery window is over."""
        if self._state != "open":
            return True
        if self._remaining() > 0:
            return False
        self._state = "probing"
        return True

    def observe(self, outcome: ConnectorOutcome) -> None:
        """Take one result into account."""
        if outcome.failure == "rejected":
            return
        if outcome.failure == "transport":
            self._failures += 1
            if self._state == "probing" or self._failures >= self._threshold:
                self._state = "open"
                self._opened_at = self._clock()
            return
        self._state = "closed"
        self._failures = 0

    def _remaining(self) -> int:
        return max(0, math.ceil(self._recovery - (self._clock() - self._opened_at)))


class BaseConnector:
    """What every connector shares: its identity, its timeout and its breaker.

    The whole of the availability behavior is here, so an implementation only
    has to say what one call does, in `_perform`, and how it failed when it did.
    A call the breaker refuses never reaches `_perform`.
    """

    def __init__(
        self,
        id: str,
        resilience: ResilienceConfig | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.id = id
        self.resilience = resilience or ResilienceConfig()
        self._breaker = CircuitBreaker(
            self.resilience.failures_before_open, self.resilience.recovery_seconds, clock
        )

    @property
    def timeout(self) -> float:
        """Seconds one call may take before it is a transport failure."""
        return self.resilience.timeout_seconds

    @property
    def health(self) -> ConnectorHealth:
        """Whether this connector is being called at all, and when it may next be tried."""
        return self._breaker.health()

    def execute(self, operation: str, payload: dict) -> ConnectorOutcome:
        """Run one operation under the breaker and report what happened."""
        if not self._breaker.allows():
            return self._refuse_without_calling()
        started = time.perf_counter()
        outcome = as_outcome(self._perform(operation, payload))
        self._breaker.observe(outcome)
        elapsed = (time.perf_counter() - started) * 1000
        return outcome.model_copy(
            update={"latency_ms": int(round(elapsed)), "health": self._breaker.health()}
        )

    def _perform(self, operation: str, payload: dict) -> ConnectorResult:
        """Run the call. Every binding implements it; the breaker decides whether it runs."""
        raise NotImplementedError

    def _refuse_without_calling(self) -> ConnectorOutcome:
        health = self._breaker.health()
        return transport_failure(
            f"{self.id} is not being called: {health.consecutive_failures} calls in a row "
            "did not reach it"
        ).model_copy(update={"health": health})


def timed(fn: Callable[P, ConnectorResult]) -> Callable[P, ConnectorResult]:
    """Wrap a call so its result carries the elapsed time."""

    @functools.wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> ConnectorResult:
        started = time.perf_counter()
        result = fn(*args, **kwargs)
        elapsed = (time.perf_counter() - started) * 1000
        return result.model_copy(update={"latency_ms": int(round(elapsed))})

    return wrapper
