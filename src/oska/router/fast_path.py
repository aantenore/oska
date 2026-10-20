"""Routing a request straight to a procedure when its wording is unmistakable."""

from __future__ import annotations

from collections.abc import Callable

from oska.catalog.enrichment import examples_fingerprint
from oska.catalog.registry import Catalog
from oska.config import EnrichmentConfig, FastPathConfig
from oska.contracts.common import RouteDecision, TurnKind
from oska.contracts.skill import Skill
from oska.index.base import (
    LearnedPhrases,
    SkillIndex,
    TextSource,
    overlap_score,
    routing_source,
    tokenize,
)
from oska.router.base import RoutingContext

__all__ = ["FastPath"]

ROUTABLE_TYPES = ("runbook", "composite")


class FastPath:
    """Matches the message against the triggers of every published procedure.

    Only a close match routes, which is why the threshold is high: this layer
    exists to save a model call on the phrasings an author wrote down, not to
    guess. It runs only when the session is waiting for nothing, and any
    failure hands the turn to the layer behind instead of ending it.

    With enrichment in force the phrasings it matches include the ones derived
    from those triggers at publish time, so the saving extends to the languages
    the author did not write in; with a learned store it also matches the
    phrasings a scheduled job kept from traffic that reached a procedure. Both
    are read from disk; this layer never calls a model, whatever is beside the
    catalog.
    """

    def __init__(
        self,
        index_factory: Callable[[], SkillIndex],
        registry: Catalog,
        config: FastPathConfig,
        enrichment: EnrichmentConfig | None = None,
        learned: LearnedPhrases | None = None,
    ) -> None:
        self._index_factory = index_factory
        self._registry = registry
        self._config = config
        self._derived = enrichment if enrichment is not None and enrichment.enabled else None
        self._learned = learned
        cap = enrichment.cap_per_route if enrichment is not None else 0
        self._texts: TextSource | None = (
            routing_source(self._derived, learned, cap)
            if self._derived is not None or learned is not None
            else None
        )
        self._index: SkillIndex | None = None
        self._fingerprint: str | None = None
        self._greetings = [tokenize(route) for route in config.routes.greeting]

    def routes(self) -> list[Skill]:
        """The procedures this layer can route to."""
        return [
            skill
            for skill in self._registry.published()
            if skill.type.value in ROUTABLE_TYPES and skill.frontmatter.triggers
        ]

    def route(self, text: str, context: RoutingContext) -> RouteDecision | None:
        """Return a decision when one procedure or the greeting list matches closely."""
        if not self._config.enabled or context.pending_goal or context.pending_approval:
            return None
        try:
            greeting = self._greeting_score(text)
            self._refresh()
            index = self._index
            ranked = index.rank(text, 1) if index is not None else []
        except Exception:
            return None
        best = ranked[0] if ranked else None
        if greeting >= self._config.threshold and (best is None or greeting >= best.score):
            return RouteDecision(
                kind=TurnKind.greeting,
                runbook_id=None,
                confidence=greeting,
                source="fast_path",
            )
        if best is not None and best.score >= self._config.threshold:
            return RouteDecision(
                kind=TurnKind.new_task,
                runbook_id=best.skill_id,
                confidence=best.score,
                source="fast_path",
            )
        return None

    def _greeting_score(self, text: str) -> float:
        tokens = tokenize(text)
        if not tokens or not self._greetings:
            return 0.0
        return max(overlap_score(tokens, greeting) for greeting in self._greetings)

    def _refresh(self) -> None:
        routes = self.routes()
        fingerprint = self._registry.fingerprint()
        # The registry fingerprints the skill files alone, so a derived file
        # written beside one of them, or a learned file written under the state
        # directory, would otherwise go unnoticed.
        if self._derived is not None:
            fingerprint = f"{fingerprint}:{examples_fingerprint(routes, self._derived)}"
        if self._learned is not None:
            fingerprint = f"{fingerprint}:{self._learned.fingerprint(routes)}"
        if self._index is not None and fingerprint == self._fingerprint:
            return
        index = self._index_factory()
        index.build(routes, self._texts)
        self._index = index
        self._fingerprint = fingerprint
