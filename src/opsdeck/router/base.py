"""Routing decisions a fixed list of words can make on its own."""

from __future__ import annotations

from dataclasses import dataclass

from opsdeck.config import RouterRules
from opsdeck.contracts.common import RouteDecision, TurnKind
from opsdeck.index.base import normalise

__all__ = ["RoutingContext", "RuleRouter"]


@dataclass(frozen=True)
class RoutingContext:
    """What the session is already waiting for when the turn arrives."""

    pending_goal: bool = False
    pending_approval: bool = False
    language: str | None = None


class RuleRouter:
    """Matches the whole message against the configured words.

    A single word only means approve, reject or cancel in reply to something,
    so these rules fire only while the session is waiting. The message must be
    that word and nothing else: "yes" is an answer, "yes, and also restart the
    other one" is a new request and is left to the layers behind.
    """

    def __init__(self, config: RouterRules) -> None:
        self._approve = _token_set(config.approve_tokens)
        self._reject = _token_set(config.reject_tokens)
        self._cancel = _token_set(config.cancel_tokens)

    def route(self, text: str, state_summary: RoutingContext) -> RouteDecision | None:
        """Return a decision when the message is one of the configured words."""
        message = normalise(text)
        if not message:
            return None
        if state_summary.pending_approval:
            if message in self._approve:
                return _decision(TurnKind.approve)
            if message in self._reject:
                return _decision(TurnKind.reject)
        if (state_summary.pending_approval or state_summary.pending_goal) and (
            message in self._cancel
        ):
            return _decision(TurnKind.cancel_task)
        return None


def _decision(kind: TurnKind) -> RouteDecision:
    return RouteDecision(kind=kind, runbook_id=None, confidence=1.0, source="rule")


def _token_set(tokens: list[str]) -> set[str]:
    return {normalise(token) for token in tokens if normalise(token)}
