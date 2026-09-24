"""Running the operations of a plan, one at a time.

Reads run as soon as they are reached. A write never runs here: it is checked
against the declarations, against the ledger and against the policy, and then
it stops the plan and waits for an approval. The only write this module
performs is the one an approval has already been given for, and it records the
intent to write before the call goes out.

A call that did not reach the system at all is neither of the two things the
rest of this module deals in. It is not a result and it is not a refusal, so it
is reported as what it is: one capability that is temporarily unavailable, and
the moment it may be tried again. The plan carries on past it, because a
binding that is down takes its own operations with it and nothing else.

A field a procedure declares as a principal input is filled here, between the
plan and the policy: after the model has finished proposing operations and
before anything judges or performs one. The plan may not carry such a field at
all. A plan that carries one is refused rather than corrected, because a value
the model chose and a value the channel authenticated are not interchangeable
and the difference is the whole point of declaring the field.

A write whose answer never came is harder than either. The connector cannot
tell a request that never left from one the system applied and then failed to
acknowledge: a gateway answering 502 and a read that timed out look the same
from here, and both are classified retryable. So nothing is assumed about such
a write. The read-back the operation declares is run anyway, and what it finds
decides: the write landed, it did not land, or — when the read-back cannot be
reached either — nothing is known about it, and a write nothing is known about
is not sent again unless the system of record itself would stop the repeat.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from oska.audit.events import new_event
from oska.audit.sink import AuditSink
from oska.connectors.base import (
    Connector,
    ConnectorError,
    classification_of,
    health_of,
    is_unavailable,
)
from oska.connectors.registry import ConnectorRegistry
from oska.contracts.common import (
    ConnectorResult,
    Hydrated,
    Outcome,
    PendingApproval,
)
from oska.contracts.events import AuditEvent, EventKind
from oska.contracts.operation import PlannedOperation
from oska.contracts.plan import Plan
from oska.contracts.receipt import VerificationStatus, WriteReceipt
from oska.contracts.skill import LifecycleTransition, OperationSpec, Skill, SkillFrontmatter
from oska.engine import outcomes as build
from oska.engine.state import SessionState
from oska.governance.approval import ApprovalGate
from oska.governance.capabilities import CapabilityRegistry
from oska.governance.identity import (
    IdentityError,
    Principal,
    refused_fields,
    resolve_inputs,
)
from oska.governance.ledger import WriteLedger
from oska.governance.policy_engine import PolicyEngine
from oska.governance.verification import Verifier, read_state

__all__ = [
    "PlanExecutor",
    "StopReason",
    "TurnContext",
    "approvals_required",
    "repeat_is_stopped",
]

StopReason = Literal["completed", "approval", "denied", "invalid_plan"]

SECOND_APPROVAL = "require_second_approval"

_NOT_READ_BACK = "the system answered no, so there was nothing to read back"
_READ_BACK_LOST = "the read-back did not answer either, so what the write did is not known"
_NOTHING_DECLARED = "nothing declares what to read back for this operation"
_LOST = "the call did not complete"
_UNKNOWN_OUTCOME = (
    "{error}; the declared read-back could not be reached either, so whether the write "
    "landed is unknown, and it is not sent again in this session"
)


def approvals_required(effect: str) -> int:
    """How many approvals an effect asks for before the write may run."""
    return 2 if effect == SECOND_APPROVAL else 1


def repeat_is_stopped(
    capabilities: CapabilityRegistry,
    spec: OperationSpec | None,
    move: LifecycleTransition | None,
) -> bool:
    """Whether the system of record would itself stop a second send of this write.

    Two declarations say that it would. Idempotency fields name what makes two
    requests the same request over there, so a repeat is collapsed into the
    first. A lifecycle move the entity does not declare again from the state it
    reaches is refused over there, so a repeat is answered no rather than
    performed twice. An operation that declares neither has nothing between it
    and a duplicate, and the runtime is then the only thing that can stop one.
    """
    if spec is None:
        return False
    if spec.idempotency_fields:
        return True
    if move is None:
        return False
    entity = capabilities.lifecycle_entity(spec)
    if entity is None:
        return False
    return capabilities.move_for(entity, spec.name, move.to) is None


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
        try:
            from_principal = resolve_inputs(
                runbook, Principal.from_dict(ctx.state.principal)
            )
        except IdentityError as exc:
            ctx.record(build.invalid_plan_outcome(plan.runbook_id, [str(exc)]))
            return "invalid_plan"
        supplied = self._supplied_by_the_model(runbook, plan, start)
        if supplied:
            ctx.record(build.invalid_plan_outcome(plan.runbook_id, supplied))
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
            _fill(spec, operation.payload, from_principal)
            if not spec.write:
                self._read(ctx, hydrated, operation)
                continue
            stop = self._hold_write(ctx, runbook, plan, index, operation, spec)
            if stop is not None:
                return stop
        return "completed"

    @staticmethod
    def _supplied_by_the_model(runbook: Skill, plan: Plan, start: int) -> list[str]:
        """Why this plan may not run: it fills in what the identity fills in.

        The whole plan from here on is read before anything of it is performed,
        so a read is not executed on the strength of a plan whose write names an
        identity the person chose.
        """
        return [
            f"{runbook.id} takes {name} from the authenticated session, and the plan "
            f"supplies it to {operation.operation}"
            for operation in plan.operations[start:]
            for name in refused_fields(runbook, operation.payload)
        ]

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
        move = None
        if spec is not None:
            refusal, move = self._lifecycle_move(ctx, spec, payload)
            if refusal is not None:
                ctx.state.pending_approval = None
                return ctx.record(build.invalid_plan_outcome(pending.runbook_id, [refusal]))
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
            classification=classification_of(result),
        )
        updated = self._verify(ctx, updated or receipt, spec, payload, result, move)
        if is_unavailable(result):
            return ctx.record(self._settle_lost(ctx, pending, spec, updated, result, move))
        return ctx.record(build.receipt_outcome(updated))

    def _settle_lost(
        self,
        ctx: TurnContext,
        pending: PendingApproval,
        spec: OperationSpec | None,
        receipt: WriteReceipt,
        result: ConnectorResult,
        move: LifecycleTransition | None,
    ) -> Outcome:
        """What a write whose answer never came did, decided by its read-back alone.

        Nothing here is settled by assumption. The read-back has already run,
        whatever the write reported, and it is what says which of three things
        happened.

        A read-back that found the write in the system says it landed: the
        receipt becomes an executed, verified write and the key is closed. A
        read-back that found it did not land leaves the key open — nothing
        changed over there, and the same intent may be asked for again in this
        session. A read-back that could not be reached either leaves the
        outcome unknown, and an unknown outcome closes the key unless the
        system of record would itself stop a repeat. When the runtime cannot
        tell whether a write landed, it does not send it again.
        """
        if receipt.verification is VerificationStatus.verified:
            landed = self._ledger.update(
                receipt.receipt_id, status="executed", classification="terminal"
            )
            self._settlement(ctx, receipt, "landed", closed=True)
            return build.receipt_outcome(landed or receipt)
        if receipt.verification is VerificationStatus.failed:
            self._settlement(ctx, receipt, "did_not_land", closed=False)
            return build.unavailable_outcome(
                pending.integration_id, pending.operation, result, receipt
            )
        if repeat_is_stopped(self._capabilities, spec, move):
            self._settlement(ctx, receipt, "unknown", closed=False)
            return build.unavailable_outcome(
                pending.integration_id, pending.operation, result, receipt
            )
        unknown = self._ledger.update(
            receipt.receipt_id,
            classification="terminal",
            error=_UNKNOWN_OUTCOME.format(error=result.error or _LOST),
        )
        self._settlement(ctx, receipt, "unknown", closed=True)
        return build.receipt_outcome(unknown or receipt)

    @staticmethod
    def _settlement(
        ctx: TurnContext,
        receipt: WriteReceipt,
        found: str,
        *,
        closed: bool,
    ) -> None:
        """Record what the read-back of a lost write found and what it settled."""
        ctx.emit(
            "execute",
            {
                "integration_id": receipt.integration_id,
                "operation": receipt.operation,
                "write": True,
                "stage": "settlement",
                "receipt_id": receipt.receipt_id,
                "ledger_key": receipt.ledger_key,
                "found": found,
                "key_closed": closed,
            },
        )

    def _verify(
        self,
        ctx: TurnContext,
        receipt: WriteReceipt,
        spec: OperationSpec | None,
        payload: dict,
        result: ConnectorResult,
        move: LifecycleTransition | None = None,
    ) -> WriteReceipt:
        """Read the write back and record what that showed.

        A write the system refused is not read back: it answered, and the
        answer was no. A write whose answer never came is read back like one
        that reported success, because the failure says nothing about whether
        the system applied it.
        """
        if spec is None:
            evidence = {"reason": _NOTHING_DECLARED}
            status = VerificationStatus.unverified
        elif not result.ok and not is_unavailable(result):
            evidence = {"reason": _NOT_READ_BACK}
            status = VerificationStatus.unverified
        else:
            connector = self._connector(receipt.integration_id)
            if connector is None:
                evidence = {"reason": "no connector is bound to this integration"}
                status = VerificationStatus.unverified
            else:
                status, evidence = self._verifier.verify(spec, payload, connector, move=move)
                if not result.ok:
                    status, evidence = _unknown_if_unanswered(status, evidence)
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
        refusal, _ = self._lifecycle_move(ctx, spec, operation.payload)
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

    def _lifecycle_move(
        self,
        ctx: TurnContext,
        spec: OperationSpec,
        payload: dict,
    ) -> tuple[str | None, LifecycleTransition | None]:
        """The move this write makes, or why it is not one the object may make.

        The state is never taken from the conversation. It is what the entity's
        declared read reports about the object the write names, taken from what
        the plan has already read when that read was about this same object and
        read again when it was not.
        """
        entity = self._capabilities.lifecycle_entity(spec)
        if entity is None:
            return None, None
        name = _short_name(entity.id)
        state = self._current_state(ctx, entity, payload)
        if state is None:
            return (
                f"the current state of the {name} could not be read through "
                f"{entity.state_read}, so {spec.name} cannot be judged a legal move",
                None,
            )
        move = self._capabilities.move_for(entity, spec.name, state)
        if move is not None:
            return None, move
        legal = self._capabilities.legal_operations(entity, state)
        listed = ", ".join(legal) if legal else "none"
        return (
            f"the {name} is {state} and {spec.name} is not a legal move from there; "
            f"the moves declared from {state} are: {listed}",
            None,
        )

    def _current_state(
        self,
        ctx: TurnContext,
        entity: SkillFrontmatter,
        payload: dict,
    ) -> str | None:
        """The state the system of record holds the object the write names in.

        What an earlier read of this turn left behind is keyed by the entity's
        short name alone, so it may well be a record of another object of the
        same kind. It is used only when it is the same object: the write's own
        payload names the object, and every field of that name the record
        carries has to agree with it. Anything else — a different object, or a
        record carrying nothing to compare — is not an answer about this
        object, and the declared read is executed for the object the write
        names instead.
        """
        operation = entity.state_read
        integration_id = (
            self._capabilities.integration_for(operation) if operation else None
        )
        read_spec = (
            self._capabilities.operation_spec(integration_id, operation)
            if operation and integration_id is not None
            else None
        )
        identity = _identity(entity, read_spec, payload)
        held = ctx.entities.get(_short_name(entity.id))
        if identity and isinstance(held, dict):
            state = read_state(entity, {_short_name(entity.id): held}, identity)
            if state is not None:
                return state
        if not operation or integration_id is None:
            return None
        result = self._call(ctx, integration_id, operation, _narrowed(read_spec, payload))
        if not result.ok:
            return None
        return read_state(entity, result.data)

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


def _short_name(entity_id: str) -> str:
    """The record key a read returns an entity under: `entity.alert` is `alert`."""
    return entity_id.split(".", 1)[-1]


def _fill(spec: OperationSpec, payload: dict, from_principal: dict) -> list[str]:
    """Put the values the identity supplies into one payload, and name what moved.

    Only the fields this operation declares are filled: a procedure binds a
    field to the identity once, and the operations that accept that field take
    it while the rest are left as the plan wrote them.
    """
    properties = spec.input_schema.get("properties")
    names = set(properties) if isinstance(properties, dict) else set()
    filled: list[str] = []
    for name, value in from_principal.items():
        if name in names and payload.get(name) != value:
            payload[name] = value
            filled.append(name)
    return filled


def _unknown_if_unanswered(
    status: VerificationStatus, evidence: dict
) -> tuple[VerificationStatus, dict]:
    """A read-back that did not answer says nothing, least of all that a write failed.

    The verifier compares what it read against what the operation declares, and
    a read it could not perform fails every comparison. That is the right answer
    for a write that reported success and the wrong one for a write whose own
    answer was lost: there, a read-back that did not answer leaves the outcome
    unknown, and only a read-back that answered and contradicted the write says
    that nothing landed.
    """
    if status is VerificationStatus.failed and evidence.get("ok") is not True:
        return VerificationStatus.unverified, {**evidence, "reason": _READ_BACK_LOST}
    return status, evidence


def _narrowed(spec: OperationSpec | None, payload: dict) -> dict:
    """The fields of a write payload the read that follows it accepts."""
    if spec is None:
        return dict(payload)
    properties = spec.input_schema.get("properties")
    if not isinstance(properties, dict):
        return {}
    return {name: value for name, value in payload.items() if name in properties}


def _identity(
    entity: SkillFrontmatter,
    read_spec: OperationSpec | None,
    payload: dict,
) -> dict:
    """Which object this write payload names, as far as the declarations say.

    Two kinds of field name the object: one the entity declares an identifier,
    and one the declared read accepts, because that is the field the read is
    narrowed by and so the field that decides which object came back. A payload
    that carries neither names no object, and nothing already read may be taken
    for it.
    """
    names = {field.name for field in entity.fields if field.identifier}
    if read_spec is not None:
        names |= set(_narrowed(read_spec, payload))
    return {name: value for name, value in payload.items() if name in names}
