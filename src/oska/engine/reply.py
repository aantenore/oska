"""The text a turn answers with.

Every reply has a deterministic form built from the catalog and the outcomes of
the turn, said in the language of the conversation from the phrase catalog,
phrase by phrase, and in the default language where a phrase has no
translation. A model, when one is configured, only rephrases the outcome summary;
what the runtime says about itself (the question it asks, the exact payload it
is asking approval for) is never handed to a model, because that text has to
match the declarations word for word.

The answer to a request outside the catalog is the one exception, and a narrow
one. It says two things: that the request cannot be done here, and what can.
Both are derived from the catalog, and a person who wrote in another language
should read them in that language. So when a model is configured, the scope is
handed to the reply writer as a fact, in the one reply call the turn makes
anyway, and the deterministic form is what the turn answers with whenever that
call does not return text. No call is added for it.

What the turn wrote is in the same category. Every reply carries the
deterministic receipt line of every write the turn performed or replayed,
appended after its own text, so the rule that a reply states writes only from
receipts holds by construction rather than by instruction. That includes a turn
that wrote and then stopped for something else: the approval it asks for next
does not excuse it from naming what it has already done.

A written reply may not claim what the receipts do not. The reply writer is
handed, per write of the turn, whether it landed; and when any write of the
turn did not land — refused, never sent, not confirmed by its read-back, or
unknown — the text it returns is not used and the deterministic form speaks
instead. The call is still made, because it is also the call that puts the
offered actions into the language of the turn; only its text is set aside.

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

Who the runtime answers as reaches the reply writer the same way. Where the
serving domain declares a host, its name, its voice and the languages it is
declared in go with every written reply as facts, in the call the turn makes
anyway; nothing is added for them and nothing else reads them. Where no host is
declared, the reply writer is told nothing about one.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from oska.catalog.resolver import CapabilityResolver, SuggestedAction
from oska.config import ReplyConfig
from oska.contracts.common import Message, Outcome, PendingInfo
from oska.contracts.platform import HostIdentity
from oska.engine.outcomes import (
    landing_facts,
    outcome_summary,
    receipt_lines,
    unavailable_instruction,
    writes_all_landed,
)
from oska.engine.phrases import Phrases
from oska.engine.state import SessionState
from oska.governance.guardrails import instruction_block
from oska.governance.ledger import canonical_json
from oska.improvement.preferences import read_preferences
from oska.planner.planner import Planner
from oska.planner.provider import ModelProviderError

__all__ = ["ReplyBuilder", "ReplyKind", "guided", "host_facts", "offered_lines"]

ReplyKind = Literal[
    "greeting",
    "off_scope",
    "input_question",
    "approval_request",
    "outcomes",
    "unavailable",
]

_UNSUPPORTED_INTENT = (
    "The request is outside what this installation does. Say that it cannot be done here"
    " and state what can be done, from the facts and nothing else."
)
# The only reply whose own text already lists every outcome of the turn, receipts
# included, so it is the only one no receipt line is appended to.
_STATES_ITS_OUTCOMES: tuple[str, ...] = ("outcomes",)

_HOST_NAME = "You answer as {name}."
_HOST_VOICE = "Your voice is {voice}."
_HOST_LANGUAGES = "You are declared to answer in: {languages}."


class ReplyBuilder:
    """Builds the reply, with a model when one is available and a template otherwise.

    `phrases` is the phrase catalog the deterministic form is said from; without
    one, the catalog shipped with the package is read.
    """

    def __init__(
        self,
        planner: Planner | None,
        resolver: CapabilityResolver,
        config: ReplyConfig,
        preferences_root: Path | None = None,
        host: HostIdentity | None = None,
        *,
        phrases: Phrases | None = None,
    ) -> None:
        self._planner = planner
        self._resolver = resolver
        self._config = config
        self._phrases = phrases or Phrases.load(default_language=config.default_language)
        self._preferences_root = None if preferences_root is None else Path(preferences_root)
        self._host_facts = host_facts(host)

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
        trusted = writes_all_landed(outcomes)
        spoken = self._language(state, language)
        receipts = receipt_lines(outcomes, self._phrases, spoken)
        if kind == "off_scope" and self._planner is not None:
            written = self._write(
                state,
                [],
                [],
                _UNSUPPORTED_INTENT,
                language,
                list(instructions or []),
                facts=[self._resolver.declared_scope()],
            )
            if written and trusted:
                return [written, *receipts]
        if kind == "outcomes" and self._planner is not None:
            written = self._write(
                state,
                outcomes,
                offered,
                reply_intent,
                language,
                [*(instructions or []), *unavailable_instruction(outcomes)],
                facts=landing_facts(outcomes),
            )
            if written and trusted:
                return [written, *receipts]
        text = self.template(kind, outcomes, pending, spoken)
        if kind in _STATES_ITS_OUTCOMES:
            return [text]
        return [text, *receipts]

    def template(
        self,
        kind: ReplyKind,
        outcomes: list[Outcome],
        pending: PendingInfo | None = None,
        language: str = "",
    ) -> str:
        """The deterministic form of a reply, used whenever no model writes it.

        It is said in `language`, or in the default language when none is given.
        """
        phrases = self._phrases
        spoken = language or self._config.default_language
        match kind:
            case "greeting" | "off_scope" | "unavailable":
                key = "no_planner" if kind == "unavailable" else kind
                return phrases.say(spoken, key, scope=self._resolver.declared_scope())
            case "input_question":
                return _question(pending, phrases, spoken)
            case "approval_request":
                return _approval_request(pending, phrases, spoken)
        return _outcomes(outcomes, phrases, spoken)

    def _language(self, state: SessionState, language: str) -> str:
        """The language of the turn: as reported, as the session settled, or the default."""
        return language or state.language or self._config.default_language

    def _write(
        self,
        state: SessionState,
        outcomes: list[Outcome],
        offered: list[SuggestedAction],
        reply_intent: str,
        language: str,
        instructions: list[str],
        *,
        facts: list[str] | None = None,
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
                self._language(state, language),
                guided(state.history[-self._config.conversation_window :], instructions),
                self.preferences(state.session_id),
                [*(facts or ()), *self._host_facts],
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


def host_facts(host: HostIdentity | None) -> list[str]:
    """Who the reply is written as, stated as facts; nothing when no host is declared."""
    if host is None:
        return []
    facts = [_HOST_NAME.format(name=host.name)]
    if host.voice:
        facts.append(_HOST_VOICE.format(voice=host.voice))
    if host.languages:
        facts.append(_HOST_LANGUAGES.format(languages=", ".join(host.languages)))
    return facts


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


def _question(pending: PendingInfo | None, phrases: Phrases, language: str) -> str:
    if pending is None or not pending.question:
        return phrases.say(language, "question")
    return pending.question


def _approval_request(pending: PendingInfo | None, phrases: Phrases, language: str) -> str:
    """The approval asked for, with the exact payload and the words that answer it.

    The words it tells a person to type are the catalog's, and the catalog was
    refused at load unless the approval rules accept them.
    """
    head = phrases.say(language, "approval_head")
    approval = pending.approval if pending is not None else None
    if approval is None:
        return head
    lines = [
        head,
        phrases.say(
            language,
            "approval_payload",
            operation=approval.operation,
            integration=approval.integration_id,
            payload=canonical_json(approval.exact_payload),
        ),
    ]
    if approval.approvals_required > 1:
        second = approval.approvals_given >= 1
        lines.append(
            phrases.say(language, "approval_second_of_two" if second else "approval_first_of_two")
        )
    lines.append(
        phrases.say(
            language,
            "approval_tail",
            approve=phrases.answer_word(language, "approve_word"),
            reject=phrases.answer_word(language, "reject_word"),
        )
    )
    return "\n".join(lines)


def _outcomes(outcomes: list[Outcome], phrases: Phrases, language: str) -> str:
    return "\n".join(
        outcome_summary(outcomes, phrases, language)
        or [phrases.say(language, "nothing_to_report")]
    )
