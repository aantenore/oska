"""Outcomes: what a turn actually did, and the plain lines that say so.

An outcome is the only thing a reply may be written from. Nothing here
interprets or softens a result: a write that was not confirmed by its read-back
is reported as unconfirmed, in the same words every time.

A capability that is down has a line of its own, because neither of the lines
it would otherwise borrow is true. It is not a refusal: nothing was decided
about the request. It is not a completion: nothing was done. It is the one
outcome that says when the same thing may be asked for again.

A write whose answer was lost and whose read-back could not be reached either
has a line of its own too. It did not fail: it may or may not have landed, and
a person told it failed may do it again by hand. The line says that nothing is
known, whether the runtime will send it again, and which receipt holds the
attempt. "Failed" is kept for a write the system answered and refused.

The lines are said in the language of the conversation, from the phrase
catalog; the values in them (operation, integration, payload, receipt, reason)
are inserted as they are. What is handed to a model instead, the landing facts
and the instruction about a capability that is down, stays in English and out
of the catalog: it is read by a model, not by a person.
"""

from __future__ import annotations

from collections.abc import Sequence

from oska.connectors.base import health_of
from oska.contracts.common import ConnectorResult, Outcome, PendingApproval, PolicyDecision
from oska.contracts.receipt import VerificationStatus, WriteReceipt
from oska.engine.phrases import Phrases
from oska.governance.ledger import canonical_json

__all__ = [
    "approval_pending_outcome",
    "cancelled_outcome",
    "denied_outcome",
    "invalid_plan_outcome",
    "is_write",
    "landed",
    "landing_facts",
    "ledger_hit_outcome",
    "read_outcome",
    "receipt_lines",
    "receipt_outcome",
    "rejected_outcome",
    "unconfirmed_state_outcome",
    "unavailable_instruction",
    "unavailable_outcome",
    "outcome_summary",
    "writes_all_landed",
]

_RECEIPT_KINDS = ("receipt", "ledger_hit")
_UNAVAILABLE = "unavailable"


def landed(outcome: Outcome) -> bool:
    """Whether a write outcome says the write was performed and its read-back confirmed it.

    A receipt found in the ledger counts the same way as one written here:
    what decides is what the receipt says, not which turn wrote it.
    """
    return (
        outcome.kind in _RECEIPT_KINDS
        and outcome.data.get("status") == "executed"
        and outcome.data.get("verification") == str(VerificationStatus.verified)
    )


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
    *,
    unknown: bool = False,
) -> Outcome:
    """A capability that could not be reached just now, and when it may be again.

    A receipt is carried when the operation was a write, so the attempt that
    never left is still named by the reply along with every other write.
    `unknown` says the write did leave and nothing could tell whether it
    landed: it may be asked for again only because the system of record would
    stop a repeat, and its line must not say that nothing was sent.
    """
    health = health_of(result)
    data: dict = {
        "write": receipt is not None,
        "breaker": health.state,
        "consecutive_failures": health.consecutive_failures,
        "retry_in_seconds": health.retry_in_seconds,
    }
    if unknown:
        data["unknown"] = True
    return Outcome(
        kind="unavailable",
        integration_id=integration_id,
        operation=operation,
        data=data,
        receipt_id=receipt.receipt_id if receipt else None,
        reason=result.error,
    )


def unconfirmed_state_outcome(
    pending: PendingApproval,
    read_integration_id: str,
    read_operation: str,
    result: ConnectorResult,
) -> Outcome:
    """An approved write not sent, because the state it is judged on could not be read again.

    The policy is evaluated again at approval against the state read now. A
    read that does not come back leaves the policy judging without that state,
    and a rule conditioned on it would silently stop matching. So nothing is
    sent, and the outcome says the state could not be confirmed.
    """
    health = health_of(result)
    return Outcome(
        kind="unavailable",
        integration_id=pending.integration_id,
        operation=pending.operation,
        data={
            "write": True,
            "state_unconfirmed": True,
            "read_integration_id": read_integration_id,
            "read_operation": read_operation,
            "approval_id": pending.approval_id,
            "breaker": health.state,
            "consecutive_failures": health.consecutive_failures,
            "retry_in_seconds": health.retry_in_seconds,
        },
        reason=result.error,
    )


def receipt_outcome(receipt: WriteReceipt, *, unknown: bool = False) -> Outcome:
    """What a write attempt left behind.

    `unknown` says the write left, its answer was lost and its read-back could
    not be reached either: it may or may not have landed, which is not a failure.
    """
    data: dict = {
        "status": receipt.status,
        "verification": str(receipt.verification),
        "evidence": _evidence_keys(receipt),
        "payload": receipt.payload,
        "response": receipt.response,
    }
    if unknown:
        data["unknown"] = True
    return Outcome(
        kind="receipt",
        integration_id=receipt.integration_id,
        operation=receipt.operation,
        data=data,
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


def rejected_outcome(
    pending: PendingApproval, reason: str = "", *, phrase: str | None = None
) -> Outcome:
    """A write that did not run: the user refused it, or it left what was approved."""
    data: dict = {"approval_id": pending.approval_id, "payload": pending.exact_payload}
    if phrase:
        data["phrase"] = phrase
    return Outcome(
        kind="rejected",
        integration_id=pending.integration_id,
        operation=pending.operation,
        data=data,
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


def cancelled_outcome(
    reason: str, pending: PendingApproval | None = None, *, phrase: str | None = None
) -> Outcome:
    """Something that was under way and is not any more.

    `phrase` names the catalog phrase that says it to a person; `reason` stays
    the record's own wording for the audit trail.
    """
    data: dict = {"approval_id": pending.approval_id} if pending else {}
    if phrase:
        data["phrase"] = phrase
    return Outcome(
        kind="cancelled",
        integration_id=pending.integration_id if pending else None,
        operation=pending.operation if pending else None,
        data=data,
        reason=reason,
    )


def outcome_summary(
    outcomes: Sequence[Outcome], phrases: Phrases, language: str
) -> list[str]:
    """One line per outcome, in this language, stating only what the outcome holds."""
    return [_line(outcome, phrases, language) for outcome in outcomes]


def receipt_lines(outcomes: Sequence[Outcome], phrases: Phrases, language: str) -> list[str]:
    """One line per write this turn performed, replayed, or could not send at all."""
    return [
        _line(outcome, phrases, language)
        for outcome in outcomes
        if _names_a_receipt(outcome)
    ]


def is_write(outcome: Outcome) -> bool:
    """Whether an outcome stands for a write this turn performed, replayed, or held back."""
    return outcome.kind in _RECEIPT_KINDS or (
        outcome.kind == _UNAVAILABLE and bool(outcome.data.get("write"))
    )


def writes_all_landed(outcomes: Sequence[Outcome]) -> bool:
    """Whether every write of the turn was performed and confirmed by its read-back.

    A turn with no write has nothing that did not land.
    """
    return all(landed(outcome) for outcome in outcomes if is_write(outcome))


def landing_facts(outcomes: Sequence[Outcome]) -> list[str]:
    """One fact per write of the turn, saying whether it landed, for the reply writer."""
    facts: list[str] = []
    for outcome in outcomes:
        if not is_write(outcome):
            continue
        receipt = f", receipt {outcome.receipt_id}" if outcome.receipt_id else ""
        if landed(outcome):
            facts.append(
                f"Write {outcome.operation}{receipt}: landed, confirmed by its read-back."
            )
        else:
            facts.append(
                f"Write {outcome.operation}{receipt}: did not land or is not confirmed. "
                "Do not say it was done."
            )
    return facts


def unavailable_instruction(outcomes: Sequence[Outcome]) -> list[str]:
    """What the reply writer is told about a capability that is down, one line each.

    It is handed over as an instruction rather than left to the wording of an
    outcome, because the two mistakes available here — reporting a refusal, or
    reporting a success — are both worse than saying nothing.
    """
    return [_instruction(outcome) for outcome in outcomes if outcome.kind == _UNAVAILABLE]


def _instruction(outcome: Outcome) -> str:
    if outcome.data.get("unknown"):
        return (
            f"{outcome.operation} was sent and nothing could confirm whether it landed. "
            "Say that it may or may not have taken effect and name its receipt. "
            "Do not say it was done and do not say it failed."
        )
    if outcome.data.get("state_unconfirmed"):
        return (
            f"{outcome.operation} was not sent, because the state it is judged on could not "
            f"be read again. Say that the state could not be confirmed, that nothing was "
            f"changed, and that it may be asked for again {_when(outcome)}. "
            "Do not say it was refused and do not say it was done."
        )
    return (
        f"{outcome.operation} could not be reached just now and nothing was sent to it. "
        f"Say that it is temporarily unavailable and that it may be tried again "
        f"{_when(outcome)}. Do not say it was refused and do not say it was done. "
        "Everything else in this conversation is unaffected."
    )


def _names_a_receipt(outcome: Outcome) -> bool:
    return outcome.kind in _RECEIPT_KINDS or (
        outcome.kind == _UNAVAILABLE and outcome.receipt_id is not None
    )


def _line(outcome: Outcome, phrases: Phrases, language: str) -> str:
    say = phrases.say
    operation = outcome.operation
    match outcome.kind:
        case "read":
            return _read_line(outcome, phrases, language)
        case "receipt":
            return _receipt_line(outcome, phrases, language)
        case "unavailable":
            return _unavailable_line(outcome, phrases, language)
        case "ledger_hit":
            if landed(outcome):
                return say(
                    language, "already_done", operation=operation, receipt_id=outcome.receipt_id
                )
            return say(
                language,
                "already_asked",
                operation=operation,
                receipt_id=outcome.receipt_id,
                status=outcome.data.get("status"),
                verification=outcome.data.get("verification"),
            )
        case "denied":
            return say(
                language, "denied", operation=operation, reason=outcome.reason or ""
            ).strip()
        case "rejected":
            if outcome.data.get("phrase"):
                return say(language, outcome.data["phrase"], operation=operation)
            if outcome.reason:
                return say(
                    language, "rejected_because", operation=operation, reason=outcome.reason
                )
            return say(language, "rejected", operation=operation)
        case "invalid_plan":
            issues = "; ".join(outcome.data.get("issues") or [])
            runbook_id = (
                outcome.data["runbook_id"]
                if "runbook_id" in outcome.data
                else say(language, "the_procedure")
            )
            return say(
                language, "invalid_plan", runbook_id=runbook_id, issues=issues
            ).strip()
        case "approval_pending":
            return say(
                language,
                "approval_pending",
                operation=operation,
                payload=canonical_json(outcome.data.get("payload") or {}),
            )
        case "cancelled":
            if outcome.data.get("phrase"):
                return say(language, outcome.data["phrase"])
            return outcome.reason or say(language, "cancelled")
    return say(language, "other_outcome", kind=outcome.kind)


def _unavailable_line(outcome: Outcome, phrases: Phrases, language: str) -> str:
    """Neither done nor refused: not reachable, and when it may be tried again.

    A write says so in the same breath as its receipt, because the attempt is
    in the record and the reply names every receipt the session holds.
    """
    say = phrases.say
    reason = outcome.reason or say(language, "did_not_answer")
    when = _retry(outcome, phrases, language)
    if outcome.data.get("state_unconfirmed"):
        read = outcome.data.get("read_operation") or say(language, "the_read_before_it")
        return say(
            language,
            "state_unconfirmed",
            operation=outcome.operation,
            read=read,
            reason=reason,
            when=when,
        )
    if outcome.data.get("unknown"):
        return say(
            language,
            "unavailable_unknown",
            operation=outcome.operation,
            integration=outcome.integration_id,
            reason=reason,
            when=when,
            receipt_id=outcome.receipt_id,
        )
    head = say(
        language,
        "unavailable",
        operation=outcome.operation,
        integration=outcome.integration_id,
        reason=reason,
        when=when,
    )
    if outcome.data.get("write"):
        tail = say(language, "unavailable_write", receipt_id=outcome.receipt_id)
    else:
        tail = say(language, "unavailable_read")
    return f"{head} {tail}"


def _when(outcome: Outcome) -> str:
    """How long until the capability may be tried again, in English words for a model."""
    seconds = outcome.data.get("retry_in_seconds")
    if not isinstance(seconds, int) or seconds <= 0:
        return "now"
    return f"in {seconds} second" if seconds == 1 else f"in {seconds} seconds"


def _retry(outcome: Outcome, phrases: Phrases, language: str) -> str:
    """How long until the capability may be tried again, in the words of the conversation."""
    seconds = outcome.data.get("retry_in_seconds")
    if not isinstance(seconds, int) or seconds <= 0:
        return phrases.say(language, "when_now")
    if seconds == 1:
        return phrases.say(language, "when_one_second")
    return phrases.say(language, "when_seconds", seconds=seconds)


def _read_line(outcome: Outcome, phrases: Phrases, language: str) -> str:
    if outcome.data.get("ok"):
        return phrases.say(
            language,
            "read",
            operation=outcome.operation,
            data=canonical_json(outcome.data.get("data") or {}),
        )
    error = outcome.data.get("error") or phrases.say(language, "no_reason_given")
    return phrases.say(language, "read_failed", operation=outcome.operation, error=error)


def _receipt_line(outcome: Outcome, phrases: Phrases, language: str) -> str:
    say = phrases.say
    receipt_id = outcome.receipt_id
    operation = outcome.operation
    status = outcome.data.get("status")
    verification = outcome.data.get("verification")
    if outcome.data.get("unknown"):
        return say(language, "receipt_unknown", operation=operation, receipt_id=receipt_id)
    if status == "error":
        reason = outcome.reason or say(language, "call_did_not_complete")
        return say(
            language, "receipt_failed", operation=operation, reason=reason, receipt_id=receipt_id
        )
    if verification == str(VerificationStatus.verified):
        evidence = ", ".join(outcome.data.get("evidence") or []) or say(
            language, "declared_read_back"
        )
        return say(
            language,
            "receipt_verified",
            operation=operation,
            evidence=evidence,
            receipt_id=receipt_id,
        )
    if verification == str(VerificationStatus.failed):
        return say(language, "receipt_contradicted", operation=operation, receipt_id=receipt_id)
    return say(language, "receipt_unconfirmed", operation=operation, receipt_id=receipt_id)


def _evidence_keys(receipt: WriteReceipt) -> list[str]:
    evidence = receipt.verification_evidence or {}
    comparisons = evidence.get("comparisons")
    if isinstance(comparisons, dict):
        return sorted(comparisons)
    return []
