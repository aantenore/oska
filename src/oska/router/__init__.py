"""Deciding what a turn is."""

from __future__ import annotations

from oska.contracts.common import RouteDecision, TurnKind
from oska.planner.provider import ModelProviderError
from oska.router.base import RoutingContext, RuleRouter
from oska.router.fast_path import FastPath
from oska.router.model_router import ModelRouter

__all__ = [
    "MODEL_UNAVAILABLE",
    "FastPath",
    "ModelRouter",
    "RoutingContext",
    "RuleRouter",
    "TurnRouter",
]


MODEL_UNAVAILABLE = "the model could not classify the turn"

class TurnRouter:
    """The three routing layers in order, cheapest first.

    The rules answer what a single word can answer, the fast path answers what
    an author already wrote down, and the model answers the rest. With no model
    configured an unmatched turn is treated as a new request, so discovery gets
    a chance to rank the catalog against it; so is one the model fails to
    classify, which then gets the answer the runtime gives without a model
    instead of failing the turn.
    """

    def __init__(
        self,
        rules: RuleRouter,
        fast_path: FastPath,
        model_router: ModelRouter | None = None,
    ) -> None:
        self._rules = rules
        self._fast_path = fast_path
        self._model_router = model_router

    def route(self, text: str, context: RoutingContext) -> RouteDecision:
        """Classify the turn through the first layer that can answer."""
        decision = self._rules.route(text, context)
        if decision is not None:
            return decision
        decision = self._fast_path.route(text, context)
        if decision is not None:
            return decision
        reason = ""
        if self._model_router is not None:
            try:
                return self._model_router.route(text, context)
            except ModelProviderError:
                reason = MODEL_UNAVAILABLE
        return RouteDecision(
            kind=TurnKind.new_task,
            runbook_id=None,
            confidence=0.0,
            source="rule",
            reason=reason,
        )
