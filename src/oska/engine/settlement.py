"""Sending an approved write once, and settling what it did.

The intent is taken with the ledger's `claim` before the call goes out. Two
turns approving the same write at once, in this process or in another one that
shares the ledger, cannot both send it: the one that loses the claim reports the
receipt of the one that won.

A write whose answer never came is harder than either a result or a refusal,
and the connector says which of two things happened to it. A write classified
`retryable` provably never left — the connection was refused, nothing was there
to send it on — so nothing can have changed over there: it needs no read-back
and leaves its key open. A write classified `ambiguous` left and no usable
answer came back — a read that timed out, a connection reset after the request
went out, a gateway answering 502 — and the system may have applied it. So
nothing is assumed about it. The read-back the operation declares is run, and
what it finds decides: the write landed, it did not land, or — when the
read-back cannot be reached either — nothing is known about it, and a write
nothing is known about is not sent again unless the system of record itself
would stop the repeat.
"""

from __future__ import annotations

from oska.connectors.base import (
    classification_of,
    idempotency_key,
    is_ambiguous,
    is_unavailable,
)
from oska.connectors.registry import ConnectorRegistry
from oska.contracts.common import ConnectorResult, Outcome, PendingApproval
from oska.contracts.receipt import VerificationStatus, WriteReceipt
from oska.contracts.skill import LifecycleTransition, OperationSpec
from oska.engine import outcomes as build
from oska.engine.calls import call, connector_for
from oska.engine.context import TurnContext
from oska.governance.capabilities import CapabilityRegistry
from oska.governance.verification import Verifier
from oska.storage.protocols import LedgerStore

__all__ = ["WriteSettlement", "repeat_is_stopped"]

_NOT_READ_BACK = "the system answered no, so there was nothing to read back"
_READ_BACK_LOST = "the read-back did not answer either, so what the write did is not known"
_NOTHING_DECLARED = "nothing declares what to read back for this operation"
_NEVER_LEFT = (
    "the request never left, so nothing can have changed and there was nothing to read back"
)
_LOST = "the call did not complete"
_UNKNOWN_OUTCOME = (
    "{error}; the declared read-back could not be reached either, so whether the write "
    "landed is unknown, and it is not sent again in this session"
)


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


class WriteSettlement:
    """Claims an approved write, sends it, reads it back and settles its receipt."""

    def __init__(
        self,
        capabilities: CapabilityRegistry,
        ledger: LedgerStore,
        verifier: Verifier,
        connectors: ConnectorRegistry,
    ) -> None:
        self._capabilities = capabilities
        self._ledger = ledger
        self._verifier = verifier
        self._connectors = connectors

    def send(
        self,
        ctx: TurnContext,
        pending: PendingApproval,
        spec: OperationSpec | None,
        payload: dict,
        move: LifecycleTransition | None,
    ) -> Outcome:
        """Claim the write, send it once, and record what the read-back settled."""
        key = self._ledger.key(
            pending.integration_id,
            pending.operation,
            payload,
            spec.idempotency_fields if spec else [],
        )
        intent = self._ledger.new_receipt(
            ctx.state.session_id,
            key,
            pending.integration_id,
            pending.operation,
            payload,
        )
        receipt = self._ledger.claim(intent)
        if receipt.receipt_id != intent.receipt_id:
            ctx.emit(
                "ledger_hit",
                {
                    "operation": pending.operation,
                    "receipt_id": receipt.receipt_id,
                    "ledger_key": key,
                    "verification": str(receipt.verification),
                    "stage": "claim",
                },
            )
            return ctx.record(build.ledger_hit_outcome(receipt))
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
        result = call(
            self._connectors,
            ctx,
            pending.integration_id,
            pending.operation,
            payload,
            write=True,
            idempotency_key=idempotency_key(ctx.state.session_id, key),
        )
        classification = classification_of(result) if not result.ok else None
        updated = self._ledger.update(
            receipt.receipt_id,
            status="executed" if result.ok else "error",
            response=result.data,
            error=result.error,
            classification=classification,
        )
        if classification == "retryable":
            return ctx.record(self._never_left(ctx, pending, updated or receipt, result))
        updated = self._verify(ctx, updated or receipt, spec, payload, result, move)
        if is_ambiguous(result):
            return ctx.record(self._settle_lost(ctx, pending, spec, updated, result, move))
        return ctx.record(build.receipt_outcome(updated))

    def _never_left(
        self,
        ctx: TurnContext,
        pending: PendingApproval,
        receipt: WriteReceipt,
        result: ConnectorResult,
    ) -> Outcome:
        """A write whose request provably never left: nothing to read back, the key stays open.

        The receipt stays `error` and `retryable`, records why it was not read
        back, and the same intent may be asked for again in this session.
        """
        evidence = {"reason": _NEVER_LEFT}
        ctx.emit(
            "verify",
            {
                "operation": receipt.operation,
                "receipt_id": receipt.receipt_id,
                "verification": str(VerificationStatus.unverified),
                "evidence": evidence,
            },
        )
        updated = self._ledger.update(receipt.receipt_id, evidence=evidence) or receipt
        _settlement(ctx, updated, "never_left", closed=False)
        return build.unavailable_outcome(
            pending.integration_id, pending.operation, result, updated
        )

    def _settle_lost(
        self,
        ctx: TurnContext,
        pending: PendingApproval,
        spec: OperationSpec | None,
        receipt: WriteReceipt,
        result: ConnectorResult,
        move: LifecycleTransition | None,
    ) -> Outcome:
        """What an `ambiguous` write did, decided by its read-back alone.

        The request left and no usable answer came back, so nothing here is
        settled by assumption. The read-back has already run, and it is what
        says which of three things happened.

        A read-back that found the write in the system says it landed: the
        receipt becomes an executed, verified write and the key is closed. A
        read-back that found it did not land reopens the key — the receipt
        becomes `retryable`, nothing changed over there, and the same intent may
        be asked for again in this session. A read-back that could not be
        reached either leaves the outcome unknown, and an unknown outcome closes
        the key, `terminal`, unless the system of record would itself stop a
        repeat, in which case it is reopened. When the runtime cannot tell
        whether a write landed, it does not send it again.
        """
        if receipt.verification is VerificationStatus.verified:
            landed = self._ledger.update(
                receipt.receipt_id, status="executed", classification="terminal"
            )
            _settlement(ctx, receipt, "landed", closed=True)
            return build.receipt_outcome(landed or receipt)
        if receipt.verification is VerificationStatus.failed:
            reopened = self._ledger.update(receipt.receipt_id, classification="retryable")
            _settlement(ctx, receipt, "did_not_land", closed=False)
            return build.unavailable_outcome(
                pending.integration_id, pending.operation, result, reopened or receipt
            )
        if repeat_is_stopped(self._capabilities, spec, move):
            reopened = self._ledger.update(receipt.receipt_id, classification="retryable")
            _settlement(ctx, receipt, "unknown", closed=False)
            return build.unavailable_outcome(
                pending.integration_id,
                pending.operation,
                result,
                reopened or receipt,
                unknown=True,
            )
        unknown = self._ledger.update(
            receipt.receipt_id,
            classification="terminal",
            error=_UNKNOWN_OUTCOME.format(error=result.error or _LOST),
        )
        _settlement(ctx, receipt, "unknown", closed=True)
        return build.receipt_outcome(unknown or receipt, unknown=True)

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
        answer was no. An `ambiguous` write is read back like one that reported
        success, because the failure says nothing about whether the system
        applied it.
        """
        if spec is None:
            evidence = {"reason": _NOTHING_DECLARED}
            status = VerificationStatus.unverified
        elif not result.ok and not is_unavailable(result):
            evidence = {"reason": _NOT_READ_BACK}
            status = VerificationStatus.unverified
        else:
            connector = connector_for(self._connectors, receipt.integration_id)
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


def _settlement(
    ctx: TurnContext,
    receipt: WriteReceipt,
    found: str,
    *,
    closed: bool,
) -> None:
    """Record what settled a write whose answer never came, and whether its key closed."""
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
