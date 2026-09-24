"""Capability checks, policy, approval, ledger and read-back."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from oska.catalog.registry import SkillRegistry
from oska.contracts.common import ConnectorResult
from oska.contracts.plan import Plan
from oska.contracts.receipt import VerificationStatus
from oska.contracts.skill import Skill
from oska.governance.approval import ApprovalGate, payload_hash
from oska.governance.capabilities import CapabilityRegistry
from oska.governance.ledger import WriteLedger
from oska.governance.policy_engine import PolicyEngine
from oska.governance.verification import Verifier

CONTAINER = "integration.container_runtime"
RESTART = "runbook.service_restart"

SAMPLE_INTEGRATION = {
    "id": "integration.sample",
    "type": "integration",
    "version": 1,
    "status": "published",
    "description": "A sample integration the governance tests read from, described at length.",
    "operations": [
        {
            "name": "thing.read",
            "write": False,
            "input_schema": {
                "type": "object",
                "properties": {"thing": {"type": "string"}},
            },
            "output_schema": {"type": "object"},
        },
        {
            "name": "thing.write",
            "write": True,
            "input_schema": {
                "type": "object",
                "properties": {
                    "thing": {"type": "string"},
                    "size": {"type": "integer"},
                    "note": {"type": "string"},
                },
            },
            "output_schema": {"type": "object"},
            "verify": {
                "operation": "thing.read",
                "expect": {"thing.name": "$payload.thing", "thing.state": "ready"},
            },
            "idempotency_fields": ["thing"],
        },
    ],
}


class FakeConnector:
    """A connector that answers with what the test gave it."""

    def __init__(
        self,
        result: ConnectorResult | None = None,
        error: Exception | None = None,
    ) -> None:
        self.result = result or ConnectorResult(ok=True, data={})
        self.error = error
        self.calls: list[tuple[str, dict]] = []

    def execute(self, operation: str, payload: dict) -> ConnectorResult:
        """Record the call and answer."""
        self.calls.append((operation, dict(payload)))
        if self.error is not None:
            raise self.error
        return self.result


@pytest.fixture
def ledger(tmp_path: Path) -> WriteLedger:
    """A ledger in a temporary file."""
    return WriteLedger(tmp_path / "state" / "ledger.sqlite")


@pytest.fixture
def engine(registry: SkillRegistry, ledger: WriteLedger) -> PolicyEngine:
    """A policy engine over the example catalog."""
    return PolicyEngine(registry, ledger)


@pytest.fixture
def capabilities(registry: SkillRegistry) -> CapabilityRegistry:
    """Declared operations of the example catalog."""
    return CapabilityRegistry(registry)


@pytest.fixture
def sample_policy(
    tmp_path: Path,
    write_skill: Callable[..., Path],
    runbook_frontmatter: Callable[..., dict],
) -> Callable[..., tuple[PolicyEngine, Skill]]:
    """Build a one-rule policy over a sample integration and return the engine and skill."""

    def _build(rule: dict, *, attach_policy: bool = True) -> tuple[PolicyEngine, Skill]:
        write_skill(SAMPLE_INTEGRATION)
        frontmatter = runbook_frontmatter(
            id="runbook.sample",
            integrations=["integration.sample"],
            capabilities={"reads": ["thing.read"], "writes": ["thing.write"]},
            policy="policy.json" if attach_policy else None,
        )
        write_skill(
            frontmatter,
            policy=(
                {"skill_id": "runbook.sample", "default": "deny", "rules": [rule]}
                if attach_policy
                else None
            ),
        )
        registry = SkillRegistry([tmp_path / "skills"])
        ledger = WriteLedger(tmp_path / "ledger.sqlite")
        return PolicyEngine(registry, ledger), registry.require("runbook.sample")

    return _build


# Capabilities


def test_capability_registry_reads_declared_operations(capabilities: CapabilityRegistry) -> None:
    spec = capabilities.operation_spec(CONTAINER, "service.restart")
    assert spec is not None
    assert spec.write is True
    assert spec.idempotency_fields == ["service"]
    assert capabilities.is_declared(CONTAINER, "service.status")
    assert capabilities.is_write(CONTAINER, "service.restart")
    assert not capabilities.is_write(CONTAINER, "service.status")


def test_capability_registry_refuses_the_undeclared(capabilities: CapabilityRegistry) -> None:
    assert capabilities.operation_spec(CONTAINER, "service.delete") is None
    assert not capabilities.is_declared(CONTAINER, "service.delete")
    assert not capabilities.is_declared("integration.absent", "service.status")
    assert not capabilities.is_declared("entity.service", "service.status")
    assert not capabilities.is_write(CONTAINER, "service.delete")


def test_writes_allowed_by_follows_the_procedure(
    capabilities: CapabilityRegistry, registry: SkillRegistry
) -> None:
    restart = registry.require(RESTART)
    health = registry.require("runbook.service_health")
    assert capabilities.writes_allowed_by(restart, "service.restart")
    assert not capabilities.writes_allowed_by(restart, "service.scale")
    assert not capabilities.writes_allowed_by(health, "service.restart")


def test_integration_for_scans_published_integrations(capabilities: CapabilityRegistry) -> None:
    assert capabilities.integration_for("service.restart") == CONTAINER
    assert capabilities.integration_for("alert.silence") == "integration.alerting"
    assert capabilities.integration_for("service.delete") is None


# Policy


def test_policy_loads_from_a_path_relative_to_the_skill(
    engine: PolicyEngine, registry: SkillRegistry
) -> None:
    policy = engine.load(registry.require(RESTART))
    assert policy is not None
    assert policy.skill_id == RESTART
    assert policy.default == "deny"
    assert [rule.effect for rule in policy.rules] == ["allow", "require_second_approval"]
    assert all(rule.max_per_session == 2 for rule in policy.rules)


def test_policy_is_absent_when_the_procedure_declares_none(
    engine: PolicyEngine, registry: SkillRegistry
) -> None:
    assert engine.load(registry.require("runbook.service_health")) is None


def test_read_operations_are_always_allowed(
    engine: PolicyEngine, registry: SkillRegistry
) -> None:
    decision = engine.evaluate(
        registry.require(RESTART), "service.status", {"service": "checkout"}, {}, "session"
    )
    assert decision.effect == "allow"
    assert decision.reason == "read operation"
    assert decision.rule is None


def test_an_operation_the_catalog_does_not_declare_is_denied(
    engine: PolicyEngine, registry: SkillRegistry
) -> None:
    """An operation nobody declares is not a read; the catalog has no account of it."""
    decision = engine.evaluate(
        registry.require(RESTART), "service.delete", {"service": "checkout"}, {}, "session"
    )
    assert decision.effect == "deny"
    assert decision.reason == "undeclared operation"
    assert decision.rule is None


def test_a_write_without_a_policy_is_denied(
    engine: PolicyEngine, registry: SkillRegistry
) -> None:
    skill = registry.require(RESTART)
    unpoliced = skill.model_copy(
        update={"frontmatter": skill.frontmatter.model_copy(update={"policy": None})}
    )
    decision = engine.evaluate(
        unpoliced, "service.restart", {"service": "checkout"}, {}, "session"
    )
    assert decision.effect == "deny"
    assert "no policy" in decision.reason


def test_the_first_matching_rule_decides(engine: PolicyEngine, registry: SkillRegistry) -> None:
    skill = registry.require(RESTART)
    staging = engine.evaluate(
        skill,
        "service.restart",
        {"service": "checkout"},
        {"service": {"environment": "staging"}},
        "session",
    )
    production = engine.evaluate(
        skill,
        "service.restart",
        {"service": "checkout"},
        {"service": {"environment": "production"}},
        "session",
    )
    assert staging.effect == "allow"
    assert production.effect == "require_second_approval"
    assert production.rule is not None
    assert production.reason == production.rule.reason


def test_no_matching_rule_falls_back_to_the_default(
    engine: PolicyEngine, registry: SkillRegistry
) -> None:
    decision = engine.evaluate(
        registry.require("runbook.alert_silence"),
        "alert.silence",
        {"alert_id": "a-1"},
        {},
        "session",
    )
    assert decision.effect == "deny"
    assert decision.reason == "no rule covers this operation"


def test_a_condition_beyond_the_limit_denies(
    engine: PolicyEngine, registry: SkillRegistry
) -> None:
    decision = engine.evaluate(
        registry.require("runbook.alert_silence"),
        "alert.silence",
        {"alert_id": "a-1", "minutes": 600},
        {},
        "session",
    )
    assert decision.effect == "deny"
    assert "four hours" in decision.reason


@pytest.mark.parametrize(
    ("condition", "payload", "expected"),
    [
        ({"size": {"eq": 3}}, {"size": 3}, "allow"),
        ({"size": {"eq": 3}}, {"size": 4}, "deny"),
        ({"size": {"ne": 3}}, {"size": 4}, "allow"),
        ({"size": {"ne": 3}}, {"size": 3}, "deny"),
        ({"thing": {"in": ["a", "b"]}}, {"thing": "a"}, "allow"),
        ({"thing": {"in": ["a", "b"]}}, {"thing": "c"}, "deny"),
        ({"thing": {"not_in": ["a"]}}, {"thing": "c"}, "allow"),
        ({"thing": {"not_in": ["a"]}}, {"thing": "a"}, "deny"),
        ({"size": {"lt": 5}}, {"size": 4}, "allow"),
        ({"size": {"lt": 5}}, {"size": 5}, "deny"),
        ({"size": {"lte": 5}}, {"size": 5}, "allow"),
        ({"size": {"lte": 5}}, {"size": 6}, "deny"),
        ({"size": {"gt": 5}}, {"size": 6}, "allow"),
        ({"size": {"gt": 5}}, {"size": 5}, "deny"),
        ({"size": {"gte": 5}}, {"size": 5}, "allow"),
        ({"size": {"gte": 5}}, {"size": 4}, "deny"),
        ({"size": {"gte": 5, "lte": 9}}, {"size": 7}, "allow"),
        ({"size": {"gte": 5, "lte": 9}}, {"size": 11}, "deny"),
        ({"absent": {"eq": 1}}, {"size": 1}, "deny"),
        ({"size": {"gt": 5}}, {"size": "large"}, "deny"),
    ],
)
def test_condition_operators(
    sample_policy: Callable[..., tuple[PolicyEngine, Skill]],
    condition: dict,
    payload: dict,
    expected: str,
) -> None:
    engine, skill = sample_policy(
        {"operation": "thing.write", "effect": "allow", "when": condition, "reason": "test rule"}
    )
    decision = engine.evaluate(skill, "thing.write", payload, {}, "session")
    assert decision.effect == expected


def test_a_wildcard_rule_matches_every_operation(
    sample_policy: Callable[..., tuple[PolicyEngine, Skill]],
) -> None:
    engine, skill = sample_policy({"operation": "*", "effect": "allow", "reason": "anything goes"})
    decision = engine.evaluate(skill, "thing.write", {"thing": "one"}, {}, "session")
    assert decision.effect == "allow"
    assert decision.reason == "anything goes"


def test_context_wins_over_payload_on_the_same_name(
    sample_policy: Callable[..., tuple[PolicyEngine, Skill]],
) -> None:
    engine, skill = sample_policy(
        {
            "operation": "thing.write",
            "effect": "allow",
            "when": {"thing.state": {"eq": "ready"}},
            "reason": "resolved from the record, not from the name in the payload",
        }
    )
    decision = engine.evaluate(
        skill,
        "thing.write",
        {"thing": "one"},
        {"thing": {"state": "ready"}},
        "session",
    )
    assert decision.effect == "allow"


def test_max_per_session_counts_pending_and_failed_receipts(
    registry: SkillRegistry, ledger: WriteLedger
) -> None:
    engine = PolicyEngine(registry, ledger)
    skill = registry.require(RESTART)
    payload = {"service": "checkout"}
    context = {"service": {"environment": "staging"}}

    assert engine.evaluate(skill, "service.restart", payload, context, "s").effect == "allow"

    pending = ledger.new_receipt("s", "key-1", CONTAINER, "service.restart", payload)
    ledger.record(pending)
    assert engine.evaluate(skill, "service.restart", payload, context, "s").effect == "allow"

    failed = ledger.new_receipt("s", "key-2", CONTAINER, "service.restart", payload)
    ledger.record(failed)
    ledger.update(failed.receipt_id, status="error", error="the runtime did not answer")

    decision = engine.evaluate(skill, "service.restart", payload, context, "s")
    assert decision.effect == "deny"
    assert "limit of 2" in decision.reason
    assert engine.evaluate(skill, "service.restart", payload, context, "other").effect == "allow"


def test_explain_names_the_effect_and_the_reason(
    engine: PolicyEngine, registry: SkillRegistry
) -> None:
    skill = registry.require(RESTART)
    allowed = engine.evaluate(
        skill,
        "service.restart",
        {"service": "checkout"},
        {"service": {"environment": "staging"}},
        "s",
    )
    refused = engine.evaluate(
        registry.require("runbook.alert_silence"),
        "alert.silence",
        {"alert_id": "a-1", "minutes": 600},
        {},
        "s",
    )
    assert engine.explain(allowed).startswith("Allowed: ")
    assert engine.explain(refused).startswith("Refused: ")


# Approval


def test_payload_hash_is_independent_of_key_order() -> None:
    assert payload_hash({"a": 1, "b": 2}) == payload_hash({"b": 2, "a": 1})
    assert payload_hash({"a": 1}) != payload_hash({"a": 2})


def test_an_approval_is_frozen_on_its_payload() -> None:
    gate = ApprovalGate()
    plan = Plan(runbook_id=RESTART)
    payload = {"service": "checkout"}
    pending = gate.request("s", RESTART, plan, 0, CONTAINER, "service.restart", payload)

    assert pending.approval_id
    assert pending.approvals_required == 1
    assert pending.approvals_given == 0
    assert pending.exact_payload == payload
    assert gate.check(pending, {"service": "checkout"})
    assert not gate.check(pending, {"service": "payments"})
    assert not gate.check(pending, {"service": "checkout", "force": True})

    payload["service"] = "payments"
    assert pending.exact_payload == {"service": "checkout"}


def test_a_changed_payload_needs_a_new_approval() -> None:
    gate = ApprovalGate()
    plan = Plan(runbook_id=RESTART)
    first = gate.request("s", RESTART, plan, 0, CONTAINER, "service.restart", {"service": "a"})
    second = gate.request("s", RESTART, plan, 0, CONTAINER, "service.restart", {"service": "b"})
    assert first.approval_id != second.approval_id
    assert first.payload_hash != second.payload_hash


def test_the_summary_is_derived_from_the_exact_payload() -> None:
    gate = ApprovalGate()
    summary = gate.summarize(CONTAINER, "service.restart", {"service": "checkout"})
    assert summary == 'service.restart on integration.container_runtime with {"service":"checkout"}'

    pending = gate.request(
        "s",
        RESTART,
        Plan(runbook_id=RESTART),
        1,
        CONTAINER,
        "service.restart",
        {"service": "checkout"},
        approvals_required=2,
    )
    assert pending.summary == summary
    assert pending.approvals_required == 2
    assert pending.op_index == 1


# Ledger


def test_the_ledger_key_uses_the_declared_fields_only(ledger: WriteLedger) -> None:
    restart = "service.restart"
    first = ledger.key(CONTAINER, restart, {"service": "a", "reason": "one"}, ["service"])
    second = ledger.key(CONTAINER, restart, {"service": "a", "reason": "two"}, ["service"])
    other = ledger.key(CONTAINER, restart, {"service": "b", "reason": "one"}, ["service"])
    whole = ledger.key(CONTAINER, restart, {"service": "a", "reason": "one"}, [])

    assert first == second
    assert first != other
    assert first != whole
    assert whole == ledger.key(
        CONTAINER, "service.restart", {"reason": "one", "service": "a"}, None
    )
    assert first != ledger.key(CONTAINER, "service.scale", {"service": "a"}, ["service"])


def test_a_receipt_starts_pending_and_unverified(ledger: WriteLedger) -> None:
    receipt = ledger.new_receipt("s", "k", CONTAINER, "service.restart", {"service": "a"})
    assert receipt.status == "pending"
    assert receipt.verification is VerificationStatus.unverified
    assert receipt.response is None
    assert receipt.error is None
    assert receipt.receipt_id


def test_an_uncertain_write_is_still_found_by_lookup(ledger: WriteLedger) -> None:
    key = ledger.key(CONTAINER, "service.restart", {"service": "a"}, ["service"])
    receipt = ledger.record(
        ledger.new_receipt("s", key, CONTAINER, "service.restart", {"service": "a"})
    )
    ledger.update(receipt.receipt_id, status="error", error="the runtime did not answer")

    found = ledger.lookup("s", key)
    assert found is not None
    assert found.receipt_id == receipt.receipt_id
    assert found.status == "error"
    assert found.error == "the runtime did not answer"
    assert ledger.lookup("other", key) is None


def test_update_fills_in_the_outcome(ledger: WriteLedger) -> None:
    receipt = ledger.record(
        ledger.new_receipt("s", "k", CONTAINER, "service.restart", {"service": "a"})
    )
    updated = ledger.update(
        receipt.receipt_id,
        status="executed",
        response={"ok": True},
        verification=VerificationStatus.verified,
        evidence={"comparisons": {"service.status": {"match": True}}},
    )
    assert updated is not None
    assert updated.status == "executed"
    assert updated.response == {"ok": True}
    assert updated.verification is VerificationStatus.verified
    assert updated.verification_evidence == {"comparisons": {"service.status": {"match": True}}}
    assert updated.error is None


def test_count_and_for_session_cover_every_state(ledger: WriteLedger) -> None:
    for index in range(3):
        ledger.record(
            ledger.new_receipt("s", f"k{index}", CONTAINER, "service.restart", {"service": "a"})
        )
    ledger.record(ledger.new_receipt("s", "k9", CONTAINER, "service.scale", {"service": "a"}))
    ledger.record(ledger.new_receipt("other", "k0", CONTAINER, "service.restart", {"service": "a"}))

    assert ledger.count("s", "service.restart") == 3
    assert ledger.count("s", "service.scale") == 1
    assert ledger.count("s", "service.logs") == 0
    assert len(ledger.for_session("s")) == 4
    assert [receipt.ledger_key for receipt in ledger.for_session("s")][:3] == ["k0", "k1", "k2"]


def test_the_ledger_survives_reopening_the_file(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "ledger.sqlite"
    first = WriteLedger(path)
    receipt = first.record(
        first.new_receipt("s", "k", CONTAINER, "service.restart", {"service": "a"})
    )
    first.update(receipt.receipt_id, status="executed", response={"ok": True})

    reopened = WriteLedger(path)
    found = reopened.lookup("s", "k")
    assert found is not None
    assert found.receipt_id == receipt.receipt_id
    assert found.status == "executed"
    assert found.payload == {"service": "a"}
    assert reopened.count("s", "service.restart") == 1


# Verification


def test_a_matching_read_back_verifies(capabilities: CapabilityRegistry) -> None:
    verifier = Verifier(capabilities)
    spec = capabilities.operation_spec(CONTAINER, "service.restart")
    assert spec is not None
    connector = FakeConnector(
        ConnectorResult(ok=True, data={"service": {"name": "checkout", "status": "running"}})
    )

    status, evidence = verifier.verify(spec, {"service": "checkout"}, connector)

    assert status is VerificationStatus.verified
    assert connector.calls == [("service.status", {"service": "checkout"})]
    assert evidence["payload"] == {"service": "checkout"}
    assert evidence["data"] == {"service": {"name": "checkout", "status": "running"}}
    assert evidence["comparisons"]["service.name"]["expected"] == "checkout"
    assert evidence["comparisons"]["service.status"]["expected"] == "running"
    assert all(entry["match"] for entry in evidence["comparisons"].values())


def test_the_read_back_payload_keeps_only_the_fields_the_read_accepts(
    capabilities: CapabilityRegistry,
) -> None:
    verifier = Verifier(capabilities)
    spec = capabilities.operation_spec(CONTAINER, "service.scale")
    assert spec is not None
    connector = FakeConnector(
        ConnectorResult(ok=True, data={"service": {"name": "api", "replicas": 3}})
    )

    status, evidence = verifier.verify(spec, {"service": "api", "replicas": 3}, connector)

    assert connector.calls == [("service.status", {"service": "api"})]
    assert evidence["comparisons"]["service.replicas"]["expected"] == 3
    assert status is VerificationStatus.verified


def test_a_mismatch_fails(capabilities: CapabilityRegistry) -> None:
    verifier = Verifier(capabilities)
    spec = capabilities.operation_spec(CONTAINER, "service.restart")
    assert spec is not None
    connector = FakeConnector(
        ConnectorResult(ok=True, data={"service": {"name": "checkout", "status": "stopped"}})
    )

    status, evidence = verifier.verify(spec, {"service": "checkout"}, connector)

    assert status is VerificationStatus.failed
    assert evidence["comparisons"]["service.status"]["match"] is False
    assert evidence["comparisons"]["service.status"]["actual"] == "stopped"
    assert evidence["comparisons"]["service.name"]["match"] is True


def test_a_missing_field_fails(capabilities: CapabilityRegistry) -> None:
    verifier = Verifier(capabilities)
    spec = capabilities.operation_spec(CONTAINER, "service.restart")
    assert spec is not None
    connector = FakeConnector(ConnectorResult(ok=True, data={"service": {"name": "checkout"}}))

    status, evidence = verifier.verify(spec, {"service": "checkout"}, connector)

    assert status is VerificationStatus.failed
    assert evidence["comparisons"]["service.status"]["found"] is False


def test_a_read_back_that_did_not_run_fails(capabilities: CapabilityRegistry) -> None:
    verifier = Verifier(capabilities)
    spec = capabilities.operation_spec(CONTAINER, "service.restart")
    assert spec is not None
    connector = FakeConnector(ConnectorResult(ok=False, data={}, error="the runtime refused"))

    status, evidence = verifier.verify(spec, {"service": "checkout"}, connector)

    assert status is VerificationStatus.failed
    assert evidence["error"] == "the runtime refused"


def test_a_connector_that_raises_leaves_the_write_unverified(
    capabilities: CapabilityRegistry,
) -> None:
    verifier = Verifier(capabilities)
    spec = capabilities.operation_spec(CONTAINER, "service.restart")
    assert spec is not None
    connector = FakeConnector(error=TimeoutError("no answer within the deadline"))

    status, evidence = verifier.verify(spec, {"service": "checkout"}, connector)

    assert status is VerificationStatus.unverified
    assert "TimeoutError" in evidence["error"]
    assert "comparisons" not in evidence


def test_a_list_position_is_read_back_by_path(capabilities: CapabilityRegistry) -> None:
    verifier = Verifier(capabilities)
    spec = capabilities.operation_spec("integration.alerting", "alert.silence")
    assert spec is not None
    connector = FakeConnector(
        ConnectorResult(ok=True, data={"alerts": [{"id": "a-1", "state": "silenced"}]})
    )

    status, _ = verifier.verify(spec, {"alert_id": "a-1", "minutes": 30}, connector)

    assert connector.calls == [("alert.list", {"alert_id": "a-1"})]
    assert status is VerificationStatus.verified


@pytest.fixture
def stateful_capabilities(
    tmp_path: Path,
    write_skill: Callable[..., Path],
) -> CapabilityRegistry:
    """A catalog whose entity has a lifecycle and whose write declares a transition."""
    write_skill(
        {
            "id": "entity.thing",
            "type": "entity",
            "version": 1,
            "status": "published",
            "description": "A thing with a lifecycle, used by the read-back tests here.",
            "fields": [
                {"name": "name", "type": "string", "identifier": True},
                {"name": "state", "type": "string"},
            ],
            "state_field": "state",
            "states": ["ready", "busy"],
        }
    )
    write_skill(
        {
            "id": "integration.stateful",
            "type": "integration",
            "version": 1,
            "status": "published",
            "description": "Reads a thing and moves it to ready, described at some length.",
            "entities": ["entity.thing"],
            "operations": [
                {
                    "name": "thing.read",
                    "write": False,
                    "entity": "entity.thing",
                    "input_schema": {
                        "type": "object",
                        "properties": {"thing": {"type": "string"}},
                    },
                    "output_schema": {"type": "object"},
                },
                {
                    "name": "thing.ready",
                    "write": True,
                    "entity": "entity.thing",
                    "transitions": {"state": "ready"},
                    "input_schema": {
                        "type": "object",
                        "properties": {"thing": {"type": "string"}},
                    },
                    "output_schema": {"type": "object"},
                    "verify": {
                        "operation": "thing.read",
                        "expect": {"thing.name": "$payload.thing"},
                    },
                },
            ],
        }
    )
    return CapabilityRegistry(SkillRegistry([tmp_path / "skills"]))


def test_a_declared_transition_becomes_an_expectation(
    stateful_capabilities: CapabilityRegistry,
) -> None:
    verifier = Verifier(stateful_capabilities)
    spec = stateful_capabilities.operation_spec("integration.stateful", "thing.ready")
    assert spec is not None
    assert "thing.state" not in spec.verify.expect
    connector = FakeConnector(
        ConnectorResult(ok=True, data={"thing": {"name": "one", "state": "busy"}})
    )

    status, evidence = verifier.verify(spec, {"thing": "one"}, connector)

    assert evidence["comparisons"]["thing.state"]["expected"] == "ready"
    assert evidence["comparisons"]["thing.state"]["actual"] == "busy"
    assert status is VerificationStatus.failed


def test_a_state_outside_the_declared_set_fails(
    stateful_capabilities: CapabilityRegistry,
) -> None:
    verifier = Verifier(stateful_capabilities)
    spec = stateful_capabilities.operation_spec("integration.stateful", "thing.ready")
    assert spec is not None
    connector = FakeConnector(
        ConnectorResult(ok=True, data={"thing": {"name": "one", "state": "melted"}})
    )

    status, evidence = verifier.verify(spec, {"thing": "one"}, connector)

    assert status is VerificationStatus.failed
    assert evidence["state_outside_declared_set"] == {
        "field": "thing.state",
        "actual": "melted",
        "declared": ["ready", "busy"],
    }
