"""The text a turn answers with.

Every reply has a deterministic form built from the catalog and the outcomes of
the turn. A model, when one is configured, only rephrases the outcome summary;
what the runtime says about itself (the scope, the question it asks, the exact
payload it is asking approval for) is never handed to a model, because that
text has to match the declarations word for word.

What the turn wrote is in the same category. Every reply carries the
deterministic receipt line of every write the turn performed or replayed,
appended after its own text, so the rule that a reply states writes only from
receipts holds by construction rather than by instruction. That includes a turn
that wrote and then stopped for something else: the approval it asks for next
does not excuse it from naming what it has already done.
"""

from __future__ import annotations

from typing import Literal

from opsdeck.catalog.resolver import CapabilityResolver, join_phrases
from opsdeck.config import ReplyConfig
from opsdeck.contracts.common import Outcome, PendingInfo
from opsdeck.engine.outcomes import outcome_summary, receipt_lines
from opsdeck.engine.state import SessionState
from opsdeck.governance.ledger import canonical_json
from opsdeck.planner.planner import Planner
from opsdeck.planner.provider import ModelProviderError

__all__ = ["ReplyBuilder", "ReplyKind"]

ReplyKind = Literal[
    "greeting",
    "off_scope",
    "input_question",
    "approval_request",
    "outcomes",
    "unavailable",
]

_GREETING = "Hello."
_OFF_SCOPE = "That is not something I can do here."
_NOTHING_TO_REPORT = "Nothing happened in this turn."
_NO_PLANNER = (
    "Planning a procedure needs a model provider, and none is configured. "
    "I can still tell you what this installation can do."
)
# The only reply whose own text already lists every outcome of the turn, receipts
# included, so it is the only one nothing is appended to.
_STATES_ITS_OUTCOMES: tuple[str, ...] = ("outcomes",)

_APPROVAL_HEAD = "This changes something, so it needs your approval first."
_APPROVAL_TAIL = "Reply yes to approve it, or no to leave it alone."
_FIRST_OF_TWO = "Two approvals are required: this is approval 1 of 2."
_SECOND_OF_TWO = (
    "1 of 2 approvals recorded. A second approval is required before anything runs."
)


class ReplyBuilder:
    """Builds the reply, with a model when one is available and a template otherwise."""

    def __init__(
        self,
        planner: Planner | None,
        resolver: CapabilityResolver,
        config: ReplyConfig,
    ) -> None:
        self._planner = planner
        self._resolver = resolver
        self._config = config

    def build(
        self,
        kind: ReplyKind,
        state: SessionState,
        outcomes: list[Outcome],
        pending: PendingInfo | None = None,
        suggestions: list[str] | None = None,
        reply_intent: str = "",
        language: str = "",
    ) -> str:
        """The text for one turn."""
        offered = list(suggestions or [])
        if kind == "outcomes" and self._planner is not None:
            written = self._write(state, outcomes, offered, reply_intent, language)
            if written:
                return "\n".join([written, *receipt_lines(outcomes)])
        text = self.template(kind, outcomes, pending, offered)
        if kind in _STATES_ITS_OUTCOMES:
            return text
        return "\n".join([text, *receipt_lines(outcomes)])

    def template(
        self,
        kind: ReplyKind,
        outcomes: list[Outcome],
        pending: PendingInfo | None = None,
        suggestions: list[str] | None = None,
    ) -> str:
        """The deterministic form of a reply, used whenever no model writes it."""
        match kind:
            case "greeting":
                return f"{_GREETING} {self._resolver.declared_scope()}"
            case "off_scope":
                return f"{_OFF_SCOPE} {self._resolver.declared_scope()}"
            case "unavailable":
                return f"{_NO_PLANNER} {self._resolver.declared_scope()}"
            case "input_question":
                return _question(pending)
            case "approval_request":
                return _approval_request(pending)
        return _outcomes(outcomes, suggestions or [])

    def _write(
        self,
        state: SessionState,
        outcomes: list[Outcome],
        suggestions: list[str],
        reply_intent: str,
        language: str,
    ) -> str | None:
        planner = self._planner
        if planner is None:
            return None
        try:
            return planner.write_reply(
                outcomes,
                reply_intent,
                suggestions,
                language or state.language or self._config.default_language,
                state.history[-self._config.conversation_window :],
            )
        except ModelProviderError:
            return None


def _question(pending: PendingInfo | None) -> str:
    if pending is None or not pending.question:
        return "I need one more detail before I can run this."
    return pending.question


def _approval_request(pending: PendingInfo | None) -> str:
    approval = pending.approval if pending is not None else None
    if approval is None:
        return _APPROVAL_HEAD
    lines = [
        _APPROVAL_HEAD,
        f"{approval.operation} on {approval.integration_id} "
        f"with {canonical_json(approval.exact_payload)}",
    ]
    if approval.approvals_required > 1:
        lines.append(_SECOND_OF_TWO if approval.approvals_given >= 1 else _FIRST_OF_TWO)
    lines.append(_APPROVAL_TAIL)
    return "\n".join(lines)


def _outcomes(outcomes: list[Outcome], suggestions: list[str]) -> str:
    lines = outcome_summary(outcomes) or [_NOTHING_TO_REPORT]
    if suggestions:
        lines.append(f"You can {join_phrases(suggestions, 'or')}.")
    return "\n".join(lines)
