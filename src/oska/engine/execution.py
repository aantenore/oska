"""Running the operations of a plan, one at a time.

Reads run as soon as they are reached. A write never runs here: it is checked
against the declarations, against the ledger and against the policy, and then
it stops the plan and waits for an approval. The only write this module
performs is the one an approval has already been given for, and it records the
intent to write before the call goes out.

A write found in the ledger is not asked for again. When its receipt is
executed and verified it is the replay of a confirmed write, and the plan goes
on past it. When it is not, the plan stops there: what was read before it stays
reported, and nothing after it runs or asks for an approval.

A call that did not reach the system at all is not a result and it is not a
refusal. The plan carries on past it, because a binding that is down takes its
own operations with it and nothing else.

What sits either side of that loop lives in modules of its own: the fields the
identity fills in before anything is judged (`principal_inputs`), whether a
write is a legal lifecycle move (`lifecycle`), and sending an approved write
once and settling what it did (`settlement`).
"""

from __future__ import annotations

from typing import Literal

from oska.connectors.base import is_unavailable
from oska.connectors.registry import ConnectorRegistry
from oska.contracts.common import (
    ConnectorResult,
    Hydrated,
    Outcome,
    PendingApproval,
)
from oska.contracts.operation import PlannedOperation
from oska.contracts.plan import Plan
from oska.contracts.skill import OperationSpec, Skill
from oska.engine import outcomes as build
from oska.engine.calls import call
from oska.engine.context import TurnContext
from oska.engine.lifecycle import lifecycle_move
from oska.engine.principal_inputs import fill, principal_values
from oska.engine.settlement import WriteSettlement, repeat_is_stopped
from oska.governance.approval import ApprovalGate
from oska.governance.capabilities import CapabilityRegistry
from oska.governance.policy_engine import PolicyEngine
from oska.governance.verification import Verifier
from oska.storage.protocols import LedgerStore

__all__ = [
    "PlanExecutor",
    "StopReason",
    "TurnContext",
    "approvals_required",
    "repeat_is_stopped",
]

StopReason = Literal["completed", "approval", "denied", "invalid_plan", "not_landed"]
"""Why a plan stopped. `not_landed` is a write found in the ledger unconfirmed with
operations still after it; found as the last operation, the plan has simply ended."""

SECOND_APPROVAL = "require_second_approval"


def approvals_required(effect: str) -> int:
    """How many approvals an effect asks for before the write may run."""
    return 2 if effect == SECOND_APPROVAL else 1


class PlanExecutor:
    """Runs the operations of a plan under the declarations that govern them."""

    def __init__(
        self,
        capabilities: CapabilityRegistry,
        policy: PolicyEngine,
        approval: ApprovalGate,
        ledger: LedgerStore,
        verifier: Verifier,
        connectors: ConnectorRegistry,
    ) -> None:
        self._capabilities = capabilities
        self._policy = policy
        self._approval = approval
        self._ledger = ledger
        self._connectors = connectors
        self._settlement = WriteSettlement(capabilities, ledger, verifier, connectors)

    def run(
        self,
        ctx: TurnContext,
        runbook: Skill,
        hydrated: Hydrated,
        plan: Plan,
        start: int = 0,
    ) -> StopReason:
        """Work through the plan from `start` and report why it stopped."""
        from_principal = principal_values(ctx, runbook, plan, start)
        if from_principal is None:
            return "invalid_plan"
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
            fill(spec, operation.payload, from_principal)
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
        pending: PendingApproval,
    ) -> Outcome | None:
        """Read again what the plan read before the write, to judge it on current state.

        The approval was given in an earlier turn. The policy is re-evaluated
        before the write runs, and it is evaluated against what the entities
        look like now, not against what they looked like when the question was
        asked. A read that does not come back would leave the policy judging
        without the state it may be conditioned on, so the first one that does
        not is returned as an outcome saying the state could not be confirmed,
        and the write is not sent. Every read the plan performs before the
        write counts, not only the ones a rule names: which of them a condition
        depends on is not something to guess at the one moment it matters.
        """
        for operation in pending.plan.operations[: pending.op_index]:
            spec = self._capabilities.operation_spec(
                operation.integration_id, operation.operation
            )
            if spec is None or spec.write:
                continue
            result = call(
                self._connectors,
                ctx,
                operation.integration_id,
                operation.operation,
                operation.payload,
            )
            if not result.ok:
                return build.unconfirmed_state_outcome(
                    pending, operation.integration_id, operation.operation, result
                )
            self._collect(ctx, hydrated, result)
        return None

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
        move = None
        if spec is not None:
            refusal, move = lifecycle_move(
                self._capabilities, self._connectors, ctx, spec, payload
            )
            if refusal is not None:
                ctx.state.pending_approval = None
                return ctx.record(build.invalid_plan_outcome(pending.runbook_id, [refusal]))
        return self._settlement.send(ctx, pending, spec, payload, move)

    def _read(
        self,
        ctx: TurnContext,
        hydrated: Hydrated,
        operation: PlannedOperation,
    ) -> None:
        result = call(
            self._connectors, ctx, operation.integration_id, operation.operation, operation.payload
        )
        if is_unavailable(result):
            ctx.record(
                build.unavailable_outcome(
                    operation.integration_id, operation.operation, result
                )
            )
            return
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
        held = self._ledger.settled(ctx.state.session_id, key)
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
            found = ctx.record(build.ledger_hit_outcome(held))
            if build.landed(found) or index + 1 == len(plan.operations):
                return None
            return "not_landed"
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
        refusal, _ = lifecycle_move(
            self._capabilities, self._connectors, ctx, spec, operation.payload
        )
        if refusal is not None:
            ctx.record(build.invalid_plan_outcome(plan.runbook_id, [refusal]))
            return "invalid_plan"
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
