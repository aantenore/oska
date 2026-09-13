"""Running the operations of a plan, one at a time.

Reads run as soon as they are reached. A write never runs here: it is checked
against the declarations, against the ledger and against the policy, and then
it stops the plan and waits for an approval. The only write this module
performs is the one an approval has already been given for, and it records the
intent to write before the call goes out.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from opsdeck.audit.events import new_event
from opsdeck.audit.sink import AuditSink
from opsdeck.connectors.base import Connector, ConnectorError
from opsdeck.connectors.registry import ConnectorRegistry
from opsdeck.contracts.common import (
    ConnectorResult,
    Hydrated,
    Outcome,
    PendingApproval,
)
from opsdeck.contracts.events import AuditEvent, EventKind
from opsdeck.contracts.operation import PlannedOperation
from opsdeck.contracts.plan import Plan
from opsdeck.contracts.receipt import VerificationStatus, WriteReceipt
from opsdeck.contracts.skill import OperationSpec, Skill
from opsdeck.engine import outcomes as build
from opsdeck.engine.state import SessionState
from opsdeck.governance.approval import ApprovalGate
from opsdeck.governance.capabilities import CapabilityRegistry
from opsdeck.governance.ledger import WriteLedger
from opsdeck.governance.policy_engine import PolicyEngine
from opsdeck.governance.verification import Verifier

__all__ = ["PlanExecutor", "StopReason", "TurnContext", "approvals_required"]

StopReason = Literal["completed", "approval", "denied", "invalid_plan"]

SECOND_APPROVAL = "require_second_approval"


def approvals_required(effect: str) -> int:
    """How many approvals an effect asks for before the write may run."""
    return 2 if effect == SECOND_APPROVAL else 1


@dataclass
class TurnContext:
    """What one turn accumulates: its events, its outcomes and what it read."""

    state: SessionState
    turn_id: str
    audit: AuditSink
    events: list[AuditEvent] = field(default_factory=list)
    outcomes: list[Outcome] = field(default_factory=list)
    entities: dict[str, dict] = field(default_factory=dict)
    entity_ids: list[str] = field(default_factory=list)

    def emit(self, kind: EventKind, data: dict | None = None) -> AuditEvent:
        """Raise one audit event, keep it on the turn and hand it to the sink."""
        event = new_event(kind, self.state.session_id, self.turn_id, data)
        self.events.append(event)
        self.audit.emit(event)
        return event

    def record(self, outcome: Outcome) -> Outcome:
        """Add one outcome to the turn."""
        self.outcomes.append(outcome)
        return outcome


class PlanExecutor:
    """Runs the operations of a plan under the declarations that govern them."""

    def __init__(
        self,
        capabilities: CapabilityRegistry,
        policy: PolicyEngine,
        approval: ApprovalGate,
        ledger: WriteLedger,
        verifier: Verifier,
        connectors: ConnectorRegistry,
    ) -> None:
        self._capabilities = capabilities
        self._policy = policy
        self._approval = approval
        self._ledger = ledger
        self._verifier = verifier
        self._connectors = connectors

    def run(
        self,
        ctx: TurnContext,
        runbook: Skill,
        hydrated: Hydrated,
        plan: Plan,
        start: int = 0,
    ) -> StopReason:
        """Work through the plan from `start` and report why it stopped."""
        for index in range(start, len(plan.operations)):
            operation = plan.operations[index]
            spec = self._capabilities.operation_spec(
                operation.integration_id, operation.operation
            )
            if spec is None:
                ctx.record(
                    build.invalid_plan_outcome(
                        plan.runbook_id,
                        [
                            f"{operation.integration_id} declares no operation "
                            f"{operation.operation}"
                        ],
                    )
                )
                return "invalid_plan"
            if not spec.write:
                self._read(ctx, hydrated, operation)
                continue
            stop = self._hold_write(ctx, runbook, plan, index, operation, spec)
            if stop is not None:
                return stop
        return "completed"

    def refresh_context(
        self,
        ctx: TurnContext,
        hydrated: Hydrated,
        plan: Plan,
        upto: int,
    ) -> None:
        """Read again what the plan read before the write, to judge it on current state.

        The approval was given in an earlier turn. The policy is re-evaluated
        before the write runs, and it is evaluated against what the entities
        look like now, not against what they looked like when the question was
        asked.
        """
        for operation in plan.operations[:upto]:
            spec = self._capabilities.operation_spec(
                operation.integration_id, operation.operation
            )
            if spec is None or spec.write:
                continue
            result = self._call(
                ctx, operation.integration_id, operation.operation, operation.payload
            )
            self._collect(ctx, hydrated, result)

    def undeclared_write(self, runbook: Skill, pending: PendingApproval) -> str | None:
        """Why the approved write is no longer covered by the catalog, or nothing.

        The approval was given in an earlier turn and the catalog can be edited
        between the two. The declarations are read again here, so an operation
        that has since been withdrawn, turned into a read, or dropped from the
        procedure's writes cannot be executed on the strength of an old answer.
        """
        spec = self._capabilities.operation_spec(pending.integration_id, pending.operation)
        if spec is None:
            return (
                f"{pending.integration_id} declares no operation {pending.operation}"
            )
        if not spec.write:
            return f"{pending.operation} is not declared as a write"
        if not self._capabilities.writes_allowed_by(runbook, pending.operation):
            return (
                f"{runbook.id} does not declare {pending.operation} "
                "among the writes it may perform"
            )
        return None

    def execute_approved(
        self,
        ctx: TurnContext,
        hydrated: Hydrated,
        pending: PendingApproval,
    ) -> Outcome:
        """Run the write an approval was given for, then read it back.

        Nothing is recorded and nothing is sent unless the declarations still
        cover the operation: a withdrawn write leaves the ledger untouched.
        """
        issue = self.undeclared_write(hydrated.runbook, pending)
        if issue is not None:
            ctx.state.pending_approval = None
            return ctx.record(build.invalid_plan_outcome(pending.runbook_id, [issue]))
        spec = self._capabilities.operation_spec(pending.integration_id, pending.operation)
        payload = dict(pending.exact_payload)
        key = self._ledger.key(
            pending.integration_id,
            pending.operation,
            payload,
            spec.idempotency_fields if spec else [],
        )
        receipt = self._ledger.record(
            self._ledger.new_receipt(
                ctx.state.session_id,
                key,
                pending.integration_id,
                pending.operation,
                payload,
            )
        )
        ctx.state.receipts.append(receipt.receipt_id)
        ctx.emit(
            "execute",
            {
                "integration_id": pending.integration_id,
                "operation": pending.operation,
                "write": True,
                "stage": "intent",
                "receipt_id": receipt.receipt_id,
                "ledger_key": key,
            },
        )
        result = self._call(
            ctx, pending.integration_id, pending.operation, payload, write=True
        )
        updated = self._ledger.update(
            receipt.receipt_id,
            status="executed" if result.ok else "error",
            response=result.data,
            error=result.error,
        )
        updated = self._verify(ctx, updated or receipt, spec, payload, result)
        return ctx.record(build.receipt_outcome(updated))

    def _verify(
        self,
        ctx: TurnContext,
        receipt: WriteReceipt,
        spec: OperationSpec | None,
        payload: dict,
        result: ConnectorResult,
    ) -> WriteReceipt:
        if spec is None or not result.ok:
            evidence = {"reason": "the write did not report success, so nothing was read back"}
            status = VerificationStatus.unverified
        else:
            connector = self._connector(receipt.integration_id)
            if connector is None:
                evidence = {"reason": "no connector is bound to this integration"}
                status = VerificationStatus.unverified
            else:
                status, evidence = self._verifier.verify(spec, payload, connector)
        ctx.emit(
            "verify",
            {
                "operation": receipt.operation,
                "receipt_id": receipt.receipt_id,
                "verification": str(status),
                "evidence": evidence,
            },
        )
        updated = self._ledger.update(
            receipt.receipt_id, verification=status, evidence=evidence
        )
        return updated or receipt

    def _read(
        self,
        ctx: TurnContext,
        hydrated: Hydrated,
        operation: PlannedOperation,
    ) -> None:
        result = self._call(
            ctx, operation.integration_id, operation.operation, operation.payload
        )
        ctx.record(
            build.read_outcome(operation.integration_id, operation.operation, result)
        )
        self._collect(ctx, hydrated, result)

    def _hold_write(
        self,
        ctx: TurnContext,
        runbook: Skill,
        plan: Plan,
        index: int,
        operation: PlannedOperation,
        spec: OperationSpec,
    ) -> StopReason | None:
        if not self._capabilities.writes_allowed_by(runbook, operation.operation):
            ctx.record(
                build.invalid_plan_outcome(
                    plan.runbook_id,
                    [
                        f"{runbook.id} does not declare {operation.operation} "
                        "among the writes it may perform"
                    ],
                )
            )
            return "invalid_plan"
        key = self._ledger.key(
            operation.integration_id,
            operation.operation,
            operation.payload,
            spec.idempotency_fields,
        )
        held = self._ledger.lookup(ctx.state.session_id, key)
        if held is not None:
            ctx.emit(
                "ledger_hit",
                {
                    "operation": operation.operation,
                    "receipt_id": held.receipt_id,
                    "ledger_key": key,
                    "verification": str(held.verification),
                },
            )
            ctx.record(build.ledger_hit_outcome(held))
            return None
        decision = self._policy.evaluate(
            runbook,
            operation.operation,
            operation.payload,
            ctx.entities,
            ctx.state.session_id,
        )
        ctx.emit(
            "policy",
            {
                "skill_id": runbook.id,
                "operation": operation.operation,
                "effect": decision.effect,
                "reason": decision.reason,
            },
        )
        if decision.effect == "deny":
            ctx.record(
                build.denied_outcome(
                    operation.integration_id,
                    operation.operation,
                    decision,
                    self._policy.explain(decision),
                )
            )
            return "denied"
        pending = self._approval.request(
            ctx.state.session_id,
            runbook.id,
            plan,
            index,
            operation.integration_id,
            operation.operation,
            operation.payload,
            approvals_required(decision.effect),
        )
        ctx.state.pending_approval = pending
        ctx.emit(
            "approval_requested",
            {
                "approval_id": pending.approval_id,
                "operation": pending.operation,
                "payload": pending.exact_payload,
                "approvals_required": pending.approvals_required,
                "effect": decision.effect,
            },
        )
        ctx.record(build.approval_pending_outcome(pending))
        return "approval"

    def _call(
        self,
        ctx: TurnContext,
        integration_id: str,
        operation: str,
        payload: dict,
        write: bool = False,
    ) -> ConnectorResult:
        connector = self._connector(integration_id)
        if connector is None:
            result = ConnectorResult(
                ok=False, error=f"no connector is bound to {integration_id}"
            )
        else:
            try:
                result = connector.execute(operation, payload)
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
        return result

    def _connector(self, integration_id: str) -> Connector | None:
        try:
            return self._connectors.get(integration_id)
        except ConnectorError:
            return None

    @staticmethod
    def _collect(ctx: TurnContext, hydrated: Hydrated, result: ConnectorResult) -> None:
        """Keep the entity records a read returned, for the policy and the suggestions."""
        if not result.ok:
            return
        for entity in hydrated.entities:
            short = entity.id.split(".", 1)[1]
            single = result.data.get(short)
            listed = result.data.get(f"{short}s")
            touched = isinstance(single, dict) or bool(isinstance(listed, list) and listed)
            if isinstance(single, dict):
                ctx.entities[short] = single
            if touched and entity.id not in ctx.entity_ids:
                ctx.entity_ids.append(entity.id)
        ctx.state.last_entities = list(ctx.entity_ids)
