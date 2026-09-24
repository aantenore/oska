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

The guardrails the skills of the turn declare at the output point reach the
model the same way, as a system message in the conversation handed to the reply
writer. They are instructions and nothing else: what a guardrail promises to
enforce is enforced before the reply is returned, not asked for in a prompt.

A capability that could not be reached travels by both roads at once. The
deterministic form says it in its own line, and the reply writer is told in
plain words what it may not say instead: that a capability which is down was
neither refused nor done. Whichever of the two writes the reply, the reader is
told the same thing.

What the turn leaves on offer is stated the same way. The actions go to the
reply writer as a key and the wording their author declared, come back with the
wording in the language of the turn, and are then listed by the runtime, one
per line, with no sentence around them: the offer is the catalog's, the
language is the conversation's, and nothing between the two is invented. An
action no label came back for is listed in the wording it was declared in.

Presentation preferences held for the session are handed to the reply writer as
facts, and to nothing else. A preference may change how an answer reads. It may
never change what the runtime does: it is not in the planning prompt, it is not
in a policy, it is not in a payload, and there is no path from here to any of
the three.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from oska.catalog.resolver import CapabilityResolver, SuggestedAction
from oska.config import ReplyConfig
from oska.contracts.common import Message, Outcome, PendingInfo
from oska.engine.outcomes import outcome_summary, receipt_lines, unavailable_instruction
from oska.engine.state import SessionState
from oska.governance.guardrails import instruction_block
from oska.governance.ledger import canonical_json
from oska.improvement.preferences import read_preferences
from oska.planner.planner import Planner
from oska.planner.provider import ModelProviderError

__all__ = ["ReplyBuilder", "ReplyKind", "guided", "offered_lines"]

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
# included, so it is the only one no receipt line is appended to.
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
        preferences_root: Path | None = None,
    ) -> None:
        self._planner = planner
        self._resolver = resolver
        self._config = config
        self._preferences_root = None if preferences_root is None else Path(preferences_root)

    def build(
        self,
        kind: ReplyKind,
        state: SessionState,
        outcomes: list[Outcome],
        pending: PendingInfo | None = None,
        actions: list[SuggestedAction] | None = None,
        reply_intent: str = "",
        language: str = "",
        *,
        instructions: list[str] | None = None,
    ) -> str:
        """The text for one turn, and then what it leaves on offer."""
        offered = list(actions or [])
        body = self._body(
            kind, state, outcomes, pending, offered, reply_intent, language, instructions
        )
        return "\n".join([*body, *offered_lines(offered)])

    def _body(
        self,
        kind: ReplyKind,
        state: SessionState,
        outcomes: list[Outcome],
        pending: PendingInfo | None,
        offered: list[SuggestedAction],
        reply_intent: str,
        language: str,
        instructions: list[str] | None,
    ) -> list[str]:
        """What the turn says about itself, before anything it offers."""
        if kind == "outcomes" and self._planner is not None:
            written = self._write(
                state,
                outcomes,
                offered,
                reply_intent,
                language,
                [*(instructions or []), *unavailable_instruction(outcomes)],
            )
            if written:
                return [written, *receipt_lines(outcomes)]
        text = self.template(kind, outcomes, pending)
        if kind in _STATES_ITS_OUTCOMES:
            return [text]
        return [text, *receipt_lines(outcomes)]

    def template(
        self,
        kind: ReplyKind,
        outcomes: list[Outcome],
        pending: PendingInfo | None = None,
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
        return _outcomes(outcomes)

    def _write(
        self,
        state: SessionState,
        outcomes: list[Outcome],
        offered: list[SuggestedAction],
        reply_intent: str,
        language: str,
        instructions: list[str],
    ) -> str | None:
        """The written reply, with the offered actions relabeled by the same call."""
        planner = self._planner
        if planner is None:
            return None
        try:
            return planner.write_reply(
                outcomes,
                reply_intent,
                offered,
                language or state.language or self._config.default_language,
                guided(state.history[-self._config.conversation_window :], instructions),
                self.preferences(state.session_id),
            )
        except ModelProviderError:
            return None

    def preferences(self, session_id: str) -> list[str]:
        """How this session asked its answers to read, and nothing more than that.

        These reach the reply writer and nothing else. There is deliberately no
        other caller: a preference that reached the planner would be a standing
        instruction to act, which is not what anyone recorded.
        """
        if self._preferences_root is None:
            return []
        return [
            preference.text
            for preference in read_preferences(self._preferences_root, session_id)
        ]


def offered_lines(actions: Sequence[SuggestedAction]) -> list[str]:
    """One line per action the turn leaves on offer, in the language of the turn.

    There is no sentence around the list. A lead-in would be written here, in
    the language this module is written in, and would sit in front of labels
    written in the language of the conversation.
    """
    return [f"- {action.label}" for action in actions if action.label]


def guided(conversation: list[Message], prose: list[str]) -> list[Message]:
    """The conversation with the declared promises appended, when there are any.

    They travel as a system message: the model reads them, and nothing that
    inspects the last thing the user said sees anything the user did not type.
    """
    if not prose:
        return list(conversation)
    return [*conversation, Message(role="system", content=instruction_block(prose))]


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


def _outcomes(outcomes: list[Outcome]) -> str:
    return "\n".join(outcome_summary(outcomes) or [_NOTHING_TO_REPORT])
