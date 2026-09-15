"""Availability: the breaker, the classification of a failure, and what is said about it.

The hard case has a section of its own at the end. A write whose answer never
came could have been applied before the answer was lost, so nothing about it is
settled by assumption: the declared read-back runs, and what it finds — landed,
did not land, or nothing at all — decides whether the same write may be sent
again in that session.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from opsdeck.audit.sink import MemorySink
from opsdeck.catalog.registry import SkillRegistry
from opsdeck.config import ResilienceConfig, Settings
from opsdeck.connectors.base import (
    BaseConnector,
    CircuitBreaker,
    ConnectorOutcome,
    is_unavailable,
    refused,
    rejected,
    succeeded,
    transport_failure,
)
from opsdeck.connectors.http import HttpConnector
from opsdeck.connectors.mock import MockConnector
from opsdeck.connectors.registry import ConnectorRegistry
from opsdeck.connectors.shell import ShellConnector
from opsdeck.contracts.common import ConnectorResult, TurnResult
from opsdeck.contracts.receipt import VerificationStatus, WriteReceipt
from opsdeck.contracts.skill import OperationSpec, VerifySpec
from opsdeck.engine.engine import Engine
from opsdeck.engine.execution import repeat_is_stopped
from opsdeck.engine.factory import build_engine
from opsdeck.engine.outcomes import (
    receipt_lines,
    unavailable_instruction,
    unavailable_outcome,
)
from opsdeck.governance.capabilities import CapabilityRegistry
from opsdeck.governance.ledger import WriteLedger
from opsdeck.planner.scripted import ScriptedModelProvider

RUNTIME = "integration.container_runtime"
ALERTING = "integration.alerting"
RESTART_REQUEST = "the search service in staging needs a restart, it is wedged again"
STOPPED_REQUEST = "reporting in staging is down again, restart it"
UNREACHABLE = {"unreachable": True, "error": "the runtime did not answer"}
#: Unreachable from the second call on, so the policy still reads what it judges
#: the write against and the read-back that follows the write finds nothing.
UNREACHABLE_AFTER_THE_POLICY_READ = {"fail_after": 1, **UNREACHABLE}
#: A service the fixture holds running, so a read-back of a restart finds what it promised.
RUNNING = "search"
#: A service the fixture holds stopped, so a read-back of a restart contradicts it.
STOPPED = "reporting"


class Clock:
    """A clock the test moves by hand."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        """Move time forward."""
        self.now += seconds


class Counting(BaseConnector):
    """A connector that answers what the test queued and counts what reached it."""

    def __init__(
        self,
        answers: Callable[[], ConnectorResult],
        resilience: ResilienceConfig,
        clock: Callable[[], float],
    ) -> None:
        super().__init__("integration.sample", resilience, clock=clock)
        self._answers = answers
        self.calls = 0

    def _perform(self, operation: str, payload: dict) -> ConnectorResult:
        self.calls += 1
        return self._answers()


def counting(
    answer: Callable[[], ConnectorResult],
    clock: Clock,
    *,
    failures_before_open: int = 2,
    recovery_seconds: float = 30.0,
) -> Counting:
    """A counting connector with the breaker the test wants."""
    return Counting(
        answer,
        ResilienceConfig(
            failures_before_open=failures_before_open, recovery_seconds=recovery_seconds
        ),
        clock,
    )


@pytest.fixture
def connectors_config(examples_root: Path) -> Path:
    """The configuration binding the example catalog to the fixtures."""
    return examples_root / "configs" / "connectors.json"


@pytest.fixture
def runtime(connectors_config: Path) -> MockConnector:
    """The container runtime binding of the example catalog."""
    connector = ConnectorRegistry.from_config(connectors_config).get(RUNTIME)
    assert isinstance(connector, MockConnector)
    return connector


# The breaker.


def test_the_breaker_opens_after_the_declared_run_of_failures() -> None:
    clock = Clock()
    connector = counting(lambda: transport_failure("nothing answered"), clock)

    first = connector.execute("thing.read", {})
    assert connector.health.state == "closed"
    assert first.classification == "retryable"

    second = connector.execute("thing.read", {})

    assert connector.calls == 2
    assert second.classification == "retryable"
    assert connector.health.state == "open"
    assert connector.health.consecutive_failures == 2
    assert connector.health.retry_in_seconds == 30


def test_an_open_breaker_refuses_without_reaching_the_system() -> None:
    clock = Clock()
    connector = counting(lambda: transport_failure("nothing answered"), clock)
    connector.execute("thing.read", {})
    connector.execute("thing.read", {})

    refusal = connector.execute("thing.read", {})

    assert connector.calls == 2  # the third call never left
    assert refusal.ok is False
    assert refusal.classification == "retryable"
    assert "is not being called" in (refusal.error or "")
    assert is_unavailable(refusal)


def test_the_breaker_probes_once_the_recovery_window_is_over_and_closes_on_success() -> None:
    clock = Clock()
    answers: list[ConnectorResult] = [
        transport_failure("nothing answered"),
        transport_failure("nothing answered"),
        succeeded({"thing": {"state": "ready"}}),
    ]
    connector = counting(lambda: answers.pop(0), clock)
    connector.execute("thing.read", {})
    connector.execute("thing.read", {})

    clock.advance(29)
    assert connector.execute("thing.read", {}).ok is False
    assert connector.calls == 2
    assert connector.health.retry_in_seconds == 1

    clock.advance(1)
    probe = connector.execute("thing.read", {})

    assert connector.calls == 3  # the one attempt was let through
    assert probe.ok is True
    assert connector.health.state == "closed"
    assert connector.health.consecutive_failures == 0


def test_a_probe_that_fails_opens_the_breaker_again() -> None:
    clock = Clock()
    connector = counting(lambda: transport_failure("nothing answered"), clock)
    connector.execute("thing.read", {})
    connector.execute("thing.read", {})
    clock.advance(30)

    connector.execute("thing.read", {})

    assert connector.calls == 3
    assert connector.health.state == "open"
    assert connector.health.retry_in_seconds == 30
    assert connector.execute("thing.read", {}).ok is False
    assert connector.calls == 3


def test_a_refusal_from_the_system_never_opens_the_breaker(runtime: MockConnector) -> None:
    for _ in range(6):
        result = runtime.execute("service.status", {"service": "absent"})
        assert (result.ok, result.error) == (False, "not found")
        assert result.classification == "terminal"

    assert runtime.health.state == "closed"
    assert runtime.health.consecutive_failures == 0


def test_a_request_that_was_never_sent_counts_for_nothing() -> None:
    clock = Clock()
    connector = counting(lambda: rejected("the payload carries no field service"), clock)

    for _ in range(5):
        assert connector.execute("thing.read", {}).classification == "terminal"

    assert connector.health.state == "closed"
    assert connector.health.consecutive_failures == 0


def test_an_answer_ends_the_run_of_failures() -> None:
    clock = Clock()
    answers: list[ConnectorResult] = [
        transport_failure("nothing answered"),
        refused("not found"),
        transport_failure("nothing answered"),
    ]
    connector = counting(lambda: answers.pop(0), clock)

    connector.execute("thing.read", {})
    connector.execute("thing.read", {})
    connector.execute("thing.read", {})

    assert connector.health.state == "closed"
    assert connector.health.consecutive_failures == 1


def test_the_breaker_can_be_reset() -> None:
    clock = Clock()
    breaker = CircuitBreaker(1, 30.0, clock)
    breaker.observe(transport_failure("nothing answered"))
    assert breaker.state == "open"

    breaker.reset()

    assert breaker.state == "closed"
    assert breaker.allows() is True


# Classification.


def test_a_timeout_is_retryable_and_a_rejected_payload_is_terminal() -> None:
    connector = ShellConnector(
        id="integration.sample",
        operations={
            "sample.sleep": {"argv": ["sleep", "5"], "timeout": 0.05},
            "sample.echo": {"argv": ["printf", "%s", "{value}"]},
        },
    )

    timed_out = connector.execute("sample.sleep", {})
    missing = connector.execute("sample.echo", {})

    assert (timed_out.failure, timed_out.classification) == ("transport", "retryable")
    assert "timed out" in (timed_out.error or "")
    assert (missing.failure, missing.classification) == ("rejected", "terminal")
    assert connector.health.consecutive_failures == 1  # only the timeout counted


def test_a_command_that_ran_and_failed_is_terminal() -> None:
    connector = ShellConnector(
        id="integration.sample", operations={"sample.fail": {"argv": ["false"]}}
    )

    result = connector.execute("sample.fail", {})

    assert (result.failure, result.classification) == ("refused", "terminal")
    assert connector.health.state == "closed"


def test_a_server_error_is_retryable_and_a_client_error_is_terminal() -> None:
    codes = iter([503, 404])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(next(codes), json={"detail": "no"})

    connector = HttpConnector(
        id=RUNTIME,
        base_url="https://runtime.test",
        operations={"service.status": {"method": "GET", "path": "/services/{service}"}},
        transport=httpx.MockTransport(handler),
    )

    server = connector.execute("service.status", {"service": "search"})
    client = connector.execute("service.status", {"service": "search"})

    assert (server.failure, server.classification) == ("transport", "retryable")
    assert (client.failure, client.classification) == ("refused", "terminal")
    connector.close()


def test_a_connection_that_is_refused_is_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    connector = HttpConnector(
        id=RUNTIME,
        base_url="https://runtime.test",
        operations={"service.status": {"method": "GET", "path": "/services/{service}"}},
        transport=httpx.MockTransport(handler),
    )

    result = connector.execute("service.status", {"service": "search"})

    assert (result.failure, result.classification) == ("transport", "retryable")
    connector.close()


def test_an_injected_outage_is_told_apart_from_an_injected_refusal(
    runtime: MockConnector,
) -> None:
    runtime.set_behavior("service.status", {"fail": True, "error": "the runtime said no"})
    business = runtime.execute("service.status", {"service": "search"})

    runtime.set_behavior("service.status", UNREACHABLE)
    outage = runtime.execute("service.status", {"service": "search"})

    assert (business.failure, business.classification) == ("refused", "terminal")
    assert (outage.failure, outage.classification) == ("transport", "retryable")
    assert is_unavailable(outage) and not is_unavailable(business)


def test_an_injected_outage_can_start_after_a_few_calls(runtime: MockConnector) -> None:
    runtime.set_behavior("service.status", {"fail_after": 1, **UNREACHABLE})

    assert runtime.execute("service.status", {"service": "search"}).ok is True
    later = runtime.execute("service.status", {"service": "search"})

    assert later.classification == "retryable"
    assert later.error == "the runtime did not answer"


def test_the_timeout_comes_from_the_binding(tmp_path: Path) -> None:
    path = tmp_path / "connectors.json"
    path.write_text(
        '{"resilience": {"timeout_seconds": 3.5}, "integrations": {'
        '"integration.sample": {"type": "shell", "operations": {}}, '
        '"integration.other": {"type": "http", "base_url": "https://x.test", '
        '"operations": {}}}}',
        encoding="utf-8",
    )

    registry = ConnectorRegistry.from_config(path)

    assert registry.get("integration.sample").timeout == 3.5
    assert registry.get("integration.other").timeout == 3.5


def test_an_integration_may_declare_its_own_resilience(tmp_path: Path) -> None:
    path = tmp_path / "connectors.json"
    path.write_text(
        '{"resilience": {"failures_before_open": 5}, "integrations": {'
        '"integration.sample": {"type": "shell", "resilience": {"recovery_seconds": 2}, '
        '"operations": {}}}}',
        encoding="utf-8",
    )

    connector = ConnectorRegistry.from_config(path).get("integration.sample")

    assert connector.resilience.failures_before_open == 5
    assert connector.resilience.recovery_seconds == 2.0


# The ledger.


def receipt(ledger: WriteLedger, session: str = "s") -> WriteReceipt:
    """An intent recorded but not yet sent."""
    key = ledger.key(RUNTIME, "service.restart", {"service": "search"}, ["service"])
    return ledger.record(
        ledger.new_receipt(session, key, RUNTIME, "service.restart", {"service": "search"})
    )


def test_a_write_that_never_reached_the_system_settles_nothing(tmp_path: Path) -> None:
    ledger = WriteLedger(tmp_path / "ledger.sqlite")
    held = receipt(ledger)
    ledger.update(
        held.receipt_id, status="error", error="the runtime did not answer",
        classification="retryable",
    )

    assert ledger.settled("s", held.ledger_key) is None
    kept = ledger.lookup("s", held.ledger_key)
    assert kept is not None
    assert (kept.receipt_id, kept.status, kept.classification) == (
        held.receipt_id,
        "error",
        "retryable",
    )


def test_a_write_the_system_refused_keeps_the_key_closed(tmp_path: Path) -> None:
    ledger = WriteLedger(tmp_path / "ledger.sqlite")
    held = receipt(ledger)
    ledger.update(
        held.receipt_id, status="error", error="not found", classification="terminal"
    )

    settled = ledger.settled("s", held.ledger_key)

    assert settled is not None and settled.receipt_id == held.receipt_id


def test_a_write_that_ran_keeps_the_key_closed(tmp_path: Path) -> None:
    ledger = WriteLedger(tmp_path / "ledger.sqlite")
    held = receipt(ledger)
    ledger.update(held.receipt_id, status="executed", response={"service": {"name": "search"}})

    settled = ledger.settled("s", held.ledger_key)

    assert settled is not None and settled.status == "executed"


def test_a_write_that_has_not_been_answered_yet_keeps_the_key_closed(tmp_path: Path) -> None:
    """The intent is recorded before the call, and until it is answered it stands."""
    ledger = WriteLedger(tmp_path / "ledger.sqlite")
    held = receipt(ledger)

    settled = ledger.settled("s", held.ledger_key)

    assert settled is not None and settled.status == "pending"


def test_a_second_attempt_settles_the_key_the_first_left_open(tmp_path: Path) -> None:
    ledger = WriteLedger(tmp_path / "ledger.sqlite")
    first = receipt(ledger)
    ledger.update(first.receipt_id, status="error", classification="retryable")
    second = receipt(ledger)
    ledger.update(second.receipt_id, status="executed", response={})

    settled = ledger.settled("s", first.ledger_key)

    assert settled is not None and settled.receipt_id == second.receipt_id
    assert len(ledger.for_session("s")) == 2


# The outcome and the words it is reported in.


def unavailable(write: bool = False, seconds: int | None = 30) -> object:
    """An unavailable outcome as the executor would build it."""
    result = ConnectorOutcome(
        ok=False,
        error="the runtime did not answer",
        failure="transport",
        classification="retryable",
        health={"state": "open", "consecutive_failures": 3, "retry_in_seconds": seconds},
    )
    held = (
        WriteReceipt(
            receipt_id="r-1",
            session_id="s",
            ledger_key="k",
            integration_id=RUNTIME,
            operation="service.restart",
            payload={"service": "search"},
            status="error",
            verification=VerificationStatus.unverified,
            created_at=datetime.now(UTC),
            classification="retryable",
        )
        if write
        else None
    )
    return unavailable_outcome(RUNTIME, "service.restart", result, held)


def test_the_line_says_neither_refused_nor_done() -> None:
    line = receipt_lines([unavailable(write=True)])[0]

    assert "temporarily unavailable" in line
    assert "may be tried again in 30 seconds" in line
    assert "Receipt r-1 records the attempt" in line
    assert "refused" not in line
    assert "ran" not in line.split(".")[0]


def test_a_read_that_is_unavailable_names_no_receipt() -> None:
    outcome = unavailable()

    assert receipt_lines([outcome]) == []
    assert outcome.receipt_id is None
    assert outcome.data["breaker"] == "open"


def test_a_capability_that_may_be_tried_now_says_so() -> None:
    line = receipt_lines([unavailable(write=True, seconds=None)])[0]

    assert "may be tried again now" in line


def test_the_reply_writer_is_told_what_it_may_not_say() -> None:
    instruction = unavailable_instruction([unavailable()])[0]

    assert "temporarily unavailable" in instruction
    assert "Do not say it was refused and do not say it was done" in instruction
    assert unavailable_instruction([]) == []


# End to end, over the example catalog.


def settings_for(repo_root: Path, state_dir: Path) -> Settings:
    """The example installation, with its state in a temporary directory."""
    return Settings(
        skills_roots=[repo_root / "examples" / "ops" / "skills"],
        runtime_config=repo_root / "configs" / "runtime.json",
        model_config_path=repo_root / "configs" / "model.json",
        connectors_config=repo_root / "examples" / "ops" / "configs" / "connectors.json",
        schemas_dir=repo_root / "schemas",
        state_dir=state_dir,
        provider="scripted",
    )


def engine_for(repo_root: Path, state_dir: Path, script: list[dict]) -> Engine:
    """An engine over the example catalog, answering from a script."""
    return build_engine(
        settings_for(repo_root, state_dir),
        provider=ScriptedModelProvider(script),
        audit_sink=MemorySink(),
    )


def route_step(runbook_id: str) -> dict:
    """A routing answer for a request that starts a procedure."""
    return {
        "response": {
            "kind": "new_task",
            "runbook_id": runbook_id,
            "confidence": 0.9,
            "language": "en",
        }
    }


def restart_plan(service: str = RUNNING) -> dict:
    """A plan that reads the state of a service and then restarts it."""
    return {
        "response": {
            "runbook_id": "runbook.service_restart",
            "operations": [
                {
                    "integration_id": RUNTIME,
                    "operation": "service.status",
                    "payload": {"service": service},
                    "write": False,
                    "purpose": "read the current state",
                },
                {
                    "integration_id": RUNTIME,
                    "operation": "service.restart",
                    "payload": {"service": service},
                    "write": True,
                    "purpose": "restart the service",
                },
            ],
            "missing": [],
            "reply_intent": "report the restart",
        }
    }


def reply_step(text: str) -> dict:
    """A written reply."""
    return {"response": {"reply": text}}


def health_script() -> list[dict]:
    """A routing answer and a plan that reads the state of a service and its log."""
    return [
        {
            "response": {
                "kind": "new_task",
                "runbook_id": "runbook.service_health",
                "confidence": 0.9,
                "language": "en",
            }
        },
        {
            "response": {
                "runbook_id": "runbook.service_health",
                "operations": [
                    {
                        "integration_id": RUNTIME,
                        "operation": "service.status",
                        "payload": {"service": "search"},
                        "write": False,
                        "purpose": "read the current state",
                    },
                    {
                        "integration_id": RUNTIME,
                        "operation": "service.logs",
                        "payload": {"service": "search"},
                        "write": False,
                        "purpose": "read the tail of the log",
                    },
                ],
                "missing": [],
                "reply_intent": "report the health of search",
            }
        },
    ]


def test_a_read_that_cannot_reach_the_system_degrades_one_capability(
    repo_root: Path, tmp_path: Path
) -> None:
    engine = engine_for(repo_root, tmp_path, health_script())
    engine.connectors.get(RUNTIME).set_behavior("service.status", UNREACHABLE)

    result = engine.turn("outage", "how is search doing")

    assert [outcome.kind for outcome in result.outcomes] == ["unavailable", "read"]
    assert "temporarily unavailable" in result.reply
    assert engine.receipts("outage") == []
    emitted = [event for event in result.events if event.kind == "unavailable"]
    assert len(emitted) == 1
    assert emitted[0].data["operation"] == "service.status"
    assert emitted[0].data["write"] is False


# A write whose answer never came, and what the read-back makes of it.
#
# Until this section the classification decided everything: a retryable failure
# left the key open and the same write went out again on the next request. That
# is exactly the double write the ledger exists to prevent, because a gateway
# answering 502 and a read that timed out are both retryable and both can sit
# on top of a write the system applied. The read-back decides now.


def test_a_write_whose_read_back_shows_it_did_not_land_may_be_asked_for_again(
    repo_root: Path, tmp_path: Path
) -> None:
    """Only a read-back that contradicts the write leaves the key open.

    This test asked for the restart of a service the fixture holds stopped: the
    write never reached the runtime, the read-back finds the service still
    stopped, and that — not the classification — is what says nothing landed.
    """
    script = [
        route_step("runbook.service_restart"),
        restart_plan(STOPPED),
        reply_step("The restart never left, so nothing was changed."),
        route_step("runbook.service_restart"),
        restart_plan(STOPPED),
    ]
    engine = engine_for(repo_root, tmp_path, script)
    engine.turn("retry", STOPPED_REQUEST)
    engine.connectors.get(RUNTIME).set_behavior("service.restart", UNREACHABLE)

    failed = engine.turn("retry", "yes")
    engine.connectors.get(RUNTIME).set_behavior("service.restart", None)
    again = engine.turn("retry", "reporting is still down, restart it")

    assert [outcome.kind for outcome in failed.outcomes] == ["unavailable"]
    held = engine.receipts("retry")
    assert (held[0].status, held[0].classification) == ("error", "retryable")
    assert held[0].verification is VerificationStatus.failed
    assert held[0].receipt_id in failed.reply
    assert "temporarily unavailable" in failed.reply
    assert _settlement(failed) == {"found": "did_not_land", "key_closed": False}
    assert [outcome.kind for outcome in again.outcomes] == ["read", "approval_pending"]


def test_a_write_whose_read_back_finds_it_landed_is_not_sent_again(
    repo_root: Path, tmp_path: Path
) -> None:
    """The answer was lost, the read-back found the write done, and the key closed.

    The receipt says what the read-back established: an executed, verified
    write. A later request for the same thing is answered from the ledger and
    reaches no connector.
    """
    script = [
        route_step("runbook.service_restart"),
        restart_plan(),
        reply_step("The runtime did not answer, but the read-back found the restart done."),
        route_step("runbook.service_restart"),
        restart_plan(),
        reply_step("Nothing ran again: this session already asked for that restart."),
    ]
    engine = engine_for(repo_root, tmp_path, script)
    engine.turn("lost", RESTART_REQUEST)
    engine.connectors.get(RUNTIME).set_behavior("service.restart", UNREACHABLE)

    lost = engine.turn("lost", "yes")
    engine.connectors.get(RUNTIME).set_behavior("service.restart", None)
    again = engine.turn("lost", "search is still wedged, restart it")

    assert [outcome.kind for outcome in lost.outcomes] == ["receipt"]
    held = engine.receipts("lost")
    assert (held[0].status, held[0].classification) == ("executed", "terminal")
    assert held[0].verification is VerificationStatus.verified
    assert _settlement(lost) == {"found": "landed", "key_closed": True}
    assert [outcome.kind for outcome in again.outcomes] == ["read", "ledger_hit"]
    assert len(engine.receipts("lost")) == 1


def test_an_unknown_outcome_is_asked_again_only_where_the_system_would_stop_a_repeat(
    repo_root: Path, tmp_path: Path
) -> None:
    """Neither the write nor its read-back was reachable, and the operation is collapsible.

    `service.restart` declares the field that makes two requests the same
    request to the runtime, so a second send would be collapsed there. That,
    and only that, is why the key is left open while nothing is known.
    """
    script = [
        route_step("runbook.service_restart"),
        restart_plan(),
        reply_step("Neither the restart nor the read-back reached the runtime."),
        route_step("runbook.service_restart"),
        restart_plan(),
    ]
    engine = engine_for(repo_root, tmp_path, script)
    engine.turn("dark", RESTART_REQUEST)
    runtime = engine.connectors.get(RUNTIME)
    runtime.set_behavior("service.restart", UNREACHABLE)
    runtime.set_behavior("service.status", UNREACHABLE_AFTER_THE_POLICY_READ)

    dark = engine.turn("dark", "yes")
    runtime.set_behavior("service.restart", None)
    runtime.set_behavior("service.status", None)
    again = engine.turn("dark", "search is still wedged, restart it")

    held = engine.receipts("dark")[0]
    assert (held.status, held.classification) == ("error", "retryable")
    assert held.verification is VerificationStatus.unverified
    assert [outcome.kind for outcome in dark.outcomes] == ["unavailable"]
    assert _settlement(dark) == {"found": "unknown", "key_closed": False}
    assert [outcome.kind for outcome in again.outcomes] == ["read", "approval_pending"]


def test_a_write_nothing_is_known_about_is_never_sent_again(
    repo_root: Path, catalog_copy: Path, tmp_path: Path
) -> None:
    """When the runtime cannot tell, it does not repeat.

    The catalog of this test declares the restart without idempotency fields
    and the service has no lifecycle, so nothing over there would stop a second
    send. Neither the write nor the read-back reaches the runtime, and the key
    is closed on an outcome nobody knows rather than risking a second restart.
    """
    _drop_idempotency(catalog_copy)
    script = [
        route_step("runbook.service_restart"),
        restart_plan(),
        reply_step("Neither the restart nor the read-back reached the runtime."),
        route_step("runbook.service_restart"),
        restart_plan(),
        reply_step("Nothing was sent again: what that restart did is not known."),
    ]
    settings = settings_for(repo_root, tmp_path / "state").model_copy(
        update={"skills_roots": [catalog_copy]}
    )
    engine = build_engine(
        settings, provider=ScriptedModelProvider(script), audit_sink=MemorySink()
    )
    engine.turn("unknown", RESTART_REQUEST)
    runtime = engine.connectors.get(RUNTIME)
    runtime.set_behavior("service.restart", UNREACHABLE)
    runtime.set_behavior("service.status", UNREACHABLE_AFTER_THE_POLICY_READ)

    lost = engine.turn("unknown", "yes")
    runtime.set_behavior("service.restart", None)
    runtime.set_behavior("service.status", None)
    again = engine.turn("unknown", "search is still wedged, restart it")

    held = engine.receipts("unknown")
    assert (held[0].status, held[0].classification) == ("error", "terminal")
    assert held[0].verification is VerificationStatus.unverified
    assert "is unknown" in (held[0].error or "")
    assert _settlement(lost) == {"found": "unknown", "key_closed": True}
    line = receipt_lines(lost.outcomes)[0]
    assert "is unknown" in line and "not sent again" in line
    assert [outcome.kind for outcome in again.outcomes] == ["read", "ledger_hit"]
    assert len(engine.receipts("unknown")) == 1


def test_what_would_stop_a_repeat_is_read_from_the_declarations(skills_root: Path) -> None:
    """The two declarations that make a second send somebody else's problem."""
    capabilities = CapabilityRegistry(SkillRegistry([skills_root]))
    restart = capabilities.operation_spec(RUNTIME, "service.restart")
    silence = capabilities.operation_spec(ALERTING, "alert.silence")
    assert restart is not None and silence is not None
    alert = capabilities.lifecycle_entity(silence)
    assert alert is not None
    move = capabilities.move_for(alert, "alert.silence", "firing")
    bare = OperationSpec(
        name="thing.do", write=True, verify=VerifySpec(operation="thing.read")
    )

    assert repeat_is_stopped(capabilities, restart, None) is True
    assert repeat_is_stopped(capabilities, _without_idempotency(silence), move) is True
    assert repeat_is_stopped(capabilities, bare, None) is False
    assert repeat_is_stopped(capabilities, None, move) is False


def _settlement(result: TurnResult) -> dict:
    """What the turn recorded about the settlement of a write whose answer was lost."""
    found = [
        event.data
        for event in result.events
        if event.kind == "execute" and event.data.get("stage") == "settlement"
    ]
    assert len(found) == 1, found
    return {"found": found[0]["found"], "key_closed": found[0]["key_closed"]}


def _without_idempotency(spec: OperationSpec) -> OperationSpec:
    """The same operation with nothing declared about collapsing two requests."""
    return spec.model_copy(update={"idempotency_fields": []})


def _drop_idempotency(catalog: Path) -> None:
    """Undeclare the idempotency fields of the restart in a copy of the catalog."""
    path = catalog / "integration" / "container_runtime" / "v1" / "skill.md"
    text = path.read_text(encoding="utf-8")
    declared = "    idempotency_fields: [service]\n"
    assert text.count(declared) == 1
    path.write_text(text.replace(declared, ""), encoding="utf-8")


def test_a_write_the_system_answered_is_never_sent_twice(
    repo_root: Path, tmp_path: Path
) -> None:
    script = [
        route_step("runbook.service_restart"),
        restart_plan(),
        reply_step("The restart ran and the read-back confirmed it."),
        route_step("runbook.service_restart"),
        restart_plan(),
        reply_step("Nothing ran again: this session already asked for that restart."),
    ]
    engine = engine_for(repo_root, tmp_path, script)
    engine.turn("once", RESTART_REQUEST)

    engine.turn("once", "yes")
    again = engine.turn("once", "search is still wedged, restart it")

    assert [outcome.kind for outcome in again.outcomes] == ["read", "ledger_hit"]
    assert len(engine.receipts("once")) == 1
