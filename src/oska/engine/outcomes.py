"""Outcomes: what a turn actually did, and the plain lines that say so.

An outcome is the only thing a reply may be written from. Nothing here
interprets or softens a result: a write that was not confirmed by its read-back
is reported as unconfirmed, in the same words every time.

A capability that is down has a line of its own, because neither of the lines
it would otherwise borrow is true. It is not a refusal: nothing was decided
about the request. It is not a completion: nothing was done. It is the one
outcome that says when the same thing may be asked for again.
"""

from __future__ import annotations

from collections.abc import Sequence

from oska.connectors.base import health_of
from oska.contracts.common import ConnectorResult, Outcome, PendingApproval, PolicyDecision
from oska.contracts.receipt import VerificationStatus, WriteReceipt
from oska.governance.ledger import canonical_json

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
    "unavailable_instruction",
    "unavailable_outcome",
    "outcome_summary",
]

_RECEIPT_KINDS = ("receipt", "ledger_hit")
_UNAVAILABLE = "unavailable"


def read_outcome(integration_id: str, operation: str, result: ConnectorResult) -> Outcome:
    """What a read returned."""
    data: dict = {"ok": result.ok, "data": result.data}
    if result.error:
        data["error"] = result.error
    return Outcome(kind="read", integration_id=integration_id, operation=operation, data=data)


def unavailable_outcome(
    integration_id: str,
    operation: str,
    result: ConnectorResult,
    receipt: WriteReceipt | None = None,
) -> Outcome:
    """A capability that could not be reached just now, and when it may be again.

    A receipt is carried when the operation was a write, so the attempt that
    never left is still named by the reply along with every other write.
    """
    health = health_of(result)
    return Outcome(
        kind="unavailable",
        integration_id=integration_id,
        operation=operation,
        data={
            "write": receipt is not None,
            "breaker": health.state,
            "consecutive_failures": health.consecutive_failures,
            "retry_in_seconds": health.retry_in_seconds,
        },
        receipt_id=receipt.receipt_id if receipt else None,
        reason=result.error,
    )


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
    """One line per write this turn performed, replayed, or could not send at all."""
    return [_line(outcome) for outcome in outcomes if _names_a_receipt(outcome)]


def unavailable_instruction(outcomes: Sequence[Outcome]) -> list[str]:
    """What the reply writer is told about a capability that is down, one line each.

    It is handed over as an instruction rather than left to the wording of an
    outcome, because the two mistakes available here — reporting a refusal, or
    reporting a success — are both worse than saying nothing.
    """
    return [
        f"{outcome.operation} could not be reached just now and nothing was sent to it. "
        f"Say that it is temporarily unavailable and that it may be tried again "
        f"{_when(outcome)}. Do not say it was refused and do not say it was done. "
        "Everything else in this conversation is unaffected."
        for outcome in outcomes
        if outcome.kind == _UNAVAILABLE
    ]


def _names_a_receipt(outcome: Outcome) -> bool:
    return outcome.kind in _RECEIPT_KINDS or (
        outcome.kind == _UNAVAILABLE and outcome.receipt_id is not None
    )


def _line(outcome: Outcome) -> str:
    match outcome.kind:
        case "read":
            return _read_line(outcome)
        case "receipt":
            return _receipt_line(outcome)
        case "unavailable":
            return _unavailable_line(outcome)
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


def _unavailable_line(outcome: Outcome) -> str:
    """Neither done nor refused: not reachable, and when it may be tried again.

    A write says so in the same breath as its receipt, because the attempt is
    in the record and the reply names every receipt the session holds.
    """
    reason = outcome.reason or "it did not answer"
    head = (
        f"{outcome.operation} could not be reached on {outcome.integration_id}: {reason}. "
        f"That capability is temporarily unavailable and may be tried again {_when(outcome)}."
    )
    if outcome.data.get("write"):
        return (
            f"{head} Nothing was sent, so nothing was changed. "
            f"Receipt {outcome.receipt_id} records the attempt."
        )
    return f"{head} Nothing was read and nothing was changed."


def _when(outcome: Outcome) -> str:
    """How long until the capability may be tried again, in words."""
    seconds = outcome.data.get("retry_in_seconds")
    if not isinstance(seconds, int) or seconds <= 0:
        return "now"
    return f"in {seconds} second" if seconds == 1 else f"in {seconds} seconds"


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
