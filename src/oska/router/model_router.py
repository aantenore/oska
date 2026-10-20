"""Classifying a turn with a model when the fixed rules cannot."""

from __future__ import annotations

import math

from oska.catalog.resolver import CapabilityResolver
from oska.config import RouterConfig
from oska.contracts.common import Message, RouteDecision, TurnKind
from oska.planner.provider import ModelProvider
from oska.router.base import RoutingContext

__all__ = ["ROUTE_SCHEMA", "ModelRouter"]

ROUTE_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["kind", "runbook_id", "confidence", "language"],
    "properties": {
        "kind": {
            "type": "string",
            "enum": [kind.value for kind in TurnKind],
            "description": "What the user is doing in this turn.",
        },
        "runbook_id": {
            "type": ["string", "null"],
            "description": "Identifier of the procedure the turn is about, or null.",
        },
        "confidence": {
            "type": "number",
            "minimum": 0,
            "maximum": 1,
            "description": "How certain the classification is.",
        },
        "language": {
            "type": ["string", "null"],
            "description": "Language of the message as a two-letter code, or null.",
        },
    },
}

_KIND_HELP = (
    "new_task: the user asks for an outcome, including a follow-up that changes "
    "the outcome already requested.",
    "continue_task: the user pushes the procedure already under way forward "
    "without changing what it should achieve.",
    "provide_input: the user answers a question the previous turn asked.",
    "approve: the user agrees to the write that is waiting.",
    "reject: the user refuses the write that is waiting.",
    "cancel_task: the user drops what is under way without asking for anything else.",
    "greeting: the user opens the conversation without asking for anything.",
    "off_scope: the request is outside the scope stated above.",
)


class ModelRouter:
    """One classification call, answered against the declared scope.

    The prompt names the procedures the catalog offers and nothing else, so a
    request outside them is classified off_scope rather than bent onto the
    nearest procedure. A kind that is not one of the declared kinds is read as
    off_scope, and a procedure that is not in the catalog is dropped.
    """

    def __init__(
        self,
        provider: ModelProvider,
        resolver: CapabilityResolver,
        config: RouterConfig,
    ) -> None:
        self._provider = provider
        self._resolver = resolver
        self._config = config

    def route(self, text: str, context: RoutingContext) -> RouteDecision:
        """Classify the turn."""
        known = {skill.id for skill in self._resolver.available_processes()}
        raw = self._provider.complete_json(
            self._system_prompt(context),
            [Message(role="user", content=text)],
            ROUTE_SCHEMA,
        )
        return _decision(raw if isinstance(raw, dict) else {}, known)

    def _system_prompt(self, context: RoutingContext) -> str:
        processes = self._resolver.available_processes()
        catalog = "\n".join(
            f"- {skill.id}: {skill.frontmatter.description}" for skill in processes
        ) or "- none"
        rules = self._config.rules
        lines = [
            "Classify one message from a conversation about running declared procedures.",
            "",
            f"Scope: {self._resolver.declared_scope()}",
            "",
            "Procedures you may name in runbook_id:",
            catalog,
            "",
            "Kinds:",
            *(f"- {item}" for item in _KIND_HELP),
            "",
            "State of the conversation:",
            f"- a procedure is under way: {_yes_no(context.pending_goal)}",
            f"- a write is waiting for approval: {_yes_no(context.pending_approval)}",
            "",
            "Rules:",
            "- Use approve or reject only while a write is waiting for approval.",
            _token_line("Words that mean approval", rules.approve_tokens),
            _token_line("Words that mean refusal", rules.reject_tokens),
            "- A follow-up that changes the outcome the user asked for is new_task, "
            "never continue_task or provide_input.",
            "- Set runbook_id only when the message is clearly about one of the "
            "procedures listed above; otherwise use null.",
            "- Anything the listed procedures cannot achieve is off_scope.",
        ]
        return "\n".join(lines)


def _decision(raw: dict, known: set[str]) -> RouteDecision:
    runbook_id = raw.get("runbook_id")
    language = raw.get("language")
    return RouteDecision(
        kind=_kind(raw.get("kind")),
        runbook_id=runbook_id if isinstance(runbook_id, str) and runbook_id in known else None,
        confidence=_confidence(raw.get("confidence")),
        source="model",
        language=language.strip() if isinstance(language, str) and language.strip() else None,
    )


def _kind(value: object) -> TurnKind:
    try:
        return TurnKind(str(value))
    except ValueError:
        return TurnKind.off_scope


def _confidence(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(number):
        return 0.0
    return max(0.0, min(1.0, number))


def _token_line(label: str, tokens: list[str]) -> str:
    return f"- {label}: {', '.join(tokens)}." if tokens else f"- {label}: none configured."


def _yes_no(value: bool) -> str:
    return "yes" if value else "no"
