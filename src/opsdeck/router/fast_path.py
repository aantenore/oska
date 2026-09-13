"""Routing a request straight to a procedure when its wording is unmistakable."""

from __future__ import annotations

from collections.abc import Callable

from opsdeck.catalog.registry import SkillRegistry
from opsdeck.config import FastPathConfig
from opsdeck.contracts.common import RouteDecision, TurnKind
from opsdeck.contracts.skill import Skill
from opsdeck.index.base import SkillIndex, overlap_score, tokenise
from opsdeck.router.base import RoutingContext

__all__ = ["FastPath"]

ROUTABLE_TYPES = ("runbook", "composite")


class FastPath:
    """Matches the message against the triggers of every published procedure.

    Only a close match routes, which is why the threshold is high: this layer
    exists to save a model call on the phrasings an author wrote down, not to
    guess. It runs only when the session is waiting for nothing, and any
    failure hands the turn to the layer behind instead of ending it.
    """

    def __init__(
        self,
        index_factory: Callable[[], SkillIndex],
        registry: SkillRegistry,
        config: FastPathConfig,
    ) -> None:
        self._index_factory = index_factory
        self._registry = registry
        self._config = config
        self._index: SkillIndex | None = None
        self._fingerprint: str | None = None
        self._greetings = [tokenise(route) for route in config.routes.greeting]

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
        tokens = tokenise(text)
        if not tokens or not self._greetings:
            return 0.0
        return max(overlap_score(tokens, greeting) for greeting in self._greetings)

    def _refresh(self) -> None:
        fingerprint = self._registry.fingerprint()
        if self._index is not None and fingerprint == self._fingerprint:
            return
        index = self._index_factory()
        index.build(self.routes())
        self._index = index
        self._fingerprint = fingerprint
