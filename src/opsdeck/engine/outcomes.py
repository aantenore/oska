"""Outcomes: what a turn actually did, and the plain lines that say so.

An outcome is the only thing a reply may be written from. Nothing here
interprets or softens a result: a write that was not confirmed by its read-back
is reported as unconfirmed, in the same words every time.
"""

from __future__ import annotations

from collections.abc import Sequence

from opsdeck.contracts.common import ConnectorResult, Outcome, PendingApproval, PolicyDecision
from opsdeck.contracts.receipt import VerificationStatus, WriteReceipt
from opsdeck.governance.ledger import canonical_json

__all__ = [
    "approval_pending_outcome",
    "cancelled_outcome",
    "denied_outcome",
    "invalid_plan_outcome",
    "ledger_hit_outcome",
    "read_outcome",
    "receipt_lines",
    "receipt_outcome",
    "rejected_outcome",
    "outcome_summary",
]

_RECEIPT_KINDS = ("receipt", "ledger_hit")


def read_outcome(integration_id: str, operation: str, result: ConnectorResult) -> Outcome:
    """What a read returned."""
    data: dict = {"ok": result.ok, "data": result.data}
    if result.error:
        data["error"] = result.error
    return Outcome(kind="read", integration_id=integration_id, operation=operation, data=data)


def receipt_outcome(receipt: WriteReceipt) -> Outcome:
    """What a write attempt left behind."""
    return Outcome(
        kind="receipt",
        integration_id=receipt.integration_id,
        operation=receipt.operation,
        data={
            "status": receipt.status,
            "verification": str(receipt.verification),
            "evidence": _evidence_keys(receipt),
            "payload": receipt.payload,
            "response": receipt.response,
        },
        receipt_id=receipt.receipt_id,
        reason=receipt.error,
    )


def ledger_hit_outcome(receipt: WriteReceipt) -> Outcome:
    """A write this session already asked for, found in the ledger."""
    return Outcome(
        kind="ledger_hit",
        integration_id=receipt.integration_id,
        operation=receipt.operation,
        data={
            "status": receipt.status,
            "verification": str(receipt.verification),
            "payload": receipt.payload,
        },
        receipt_id=receipt.receipt_id,
    )


def denied_outcome(
    integration_id: str,
    operation: str,
    decision: PolicyDecision,
    explanation: str,
) -> Outcome:
    """A write the policy refused."""
    return Outcome(
        kind="denied",
        integration_id=integration_id,
        operation=operation,
        data={
            "effect": decision.effect,
            "rule": decision.rule.operation if decision.rule else None,
        },
        reason=explanation,
    )


def rejected_outcome(pending: PendingApproval, reason: str = "") -> Outcome:
    """A write that did not run: the user refused it, or it left what was approved."""
    return Outcome(
        kind="rejected",
        integration_id=pending.integration_id,
        operation=pending.operation,
        data={"approval_id": pending.approval_id, "payload": pending.exact_payload},
        reason=reason,
    )


def invalid_plan_outcome(runbook_id: str, issues: Sequence[str]) -> Outcome:
    """A plan that disagreed with what the catalog declares."""
    return Outcome(
        kind="invalid_plan",
        data={"runbook_id": runbook_id, "issues": list(issues)},
        reason="; ".join(issues),
    )


def approval_pending_outcome(pending: PendingApproval) -> Outcome:
    """A write held at the approval gate."""
    return Outcome(
        kind="approval_pending",
        integration_id=pending.integration_id,
        operation=pending.operation,
        data={
            "approval_id": pending.approval_id,
            "payload": pending.exact_payload,
            "approvals_required": pending.approvals_required,
            "approvals_given": pending.approvals_given,
        },
    )


def cancelled_outcome(reason: str, pending: PendingApproval | None = None) -> Outcome:
    """Something that was under way and is not any more."""
    data = {"approval_id": pending.approval_id} if pending else {}
    return Outcome(
        kind="cancelled",
        integration_id=pending.integration_id if pending else None,
        operation=pending.operation if pending else None,
        data=data,
        reason=reason,
    )


def outcome_summary(outcomes: Sequence[Outcome]) -> list[str]:
    """One English line per outcome, stating only what the outcome holds."""
    return [_line(outcome) for outcome in outcomes]


def receipt_lines(outcomes: Sequence[Outcome]) -> list[str]:
    """The lines that name a receipt: one per write this turn performed or replayed."""
    return [_line(outcome) for outcome in outcomes if outcome.kind in _RECEIPT_KINDS]


def _line(outcome: Outcome) -> str:
    match outcome.kind:
        case "read":
            return _read_line(outcome)
        case "receipt":
            return _receipt_line(outcome)
        case "ledger_hit":
            return (
                f"{outcome.operation} was already done in this session, "
                f"receipt {outcome.receipt_id}."
            )
        case "denied":
            return f"{outcome.operation} was refused. {outcome.reason or ''}".strip()
        case "rejected":
            if outcome.reason:
                return f"{outcome.operation} was not run: {outcome.reason}."
            return f"{outcome.operation} was not run, because you rejected it."
        case "invalid_plan":
            issues = "; ".join(outcome.data.get("issues") or [])
            runbook_id = outcome.data.get("runbook_id", "the procedure")
            return f"Nothing ran: the plan for {runbook_id} does not hold up. {issues}".strip()
        case "approval_pending":
            return (
                f"{outcome.operation} is waiting for your approval with "
                f"{canonical_json(outcome.data.get('payload') or {})}."
            )
        case "cancelled":
            return outcome.reason or "The request under way was cancelled."
    return f"{outcome.kind}."


def _read_line(outcome: Outcome) -> str:
    if outcome.data.get("ok"):
        return f"Read {outcome.operation}: {canonical_json(outcome.data.get('data') or {})}"
    error = outcome.data.get("error") or "no reason given"
    return f"{outcome.operation} could not be read: {error}."


def _receipt_line(outcome: Outcome) -> str:
    receipt_id = outcome.receipt_id
    operation = outcome.operation
    status = outcome.data.get("status")
    verification = outcome.data.get("verification")
    if status == "error":
        return (
            f"{operation} failed: {outcome.reason or 'the call did not complete'}. "
            f"Receipt {receipt_id} records the attempt."
        )
    if verification == str(VerificationStatus.verified):
        evidence = ", ".join(outcome.data.get("evidence") or []) or "the declared read-back"
        return (
            f"{operation} ran and the read-back confirmed {evidence}. "
            f"Receipt {receipt_id} is verified."
        )
    if verification == str(VerificationStatus.failed):
        return (
            f"{operation} ran, but the read-back contradicted it. "
            f"Receipt {receipt_id} is not verified."
        )
    return (
        f"{operation} ran, but nothing confirmed it. "
        f"Receipt {receipt_id} is not verified."
    )


def _evidence_keys(receipt: WriteReceipt) -> list[str]:
    evidence = receipt.verification_evidence or {}
    comparisons = evidence.get("comparisons")
    if isinstance(comparisons, dict):
        return sorted(comparisons)
    return []
