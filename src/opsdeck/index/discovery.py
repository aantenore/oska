"""Choosing which procedure a request is about."""

from __future__ import annotations

from opsdeck.catalog.registry import SkillRegistry
from opsdeck.config import DiscoveryConfig
from opsdeck.contracts.common import DiscoveryResult
from opsdeck.contracts.skill import Skill
from opsdeck.index.base import SkillIndex

__all__ = ["Discovery"]


class Discovery:
    """Ranks the catalog against a request and says whether the top is clear.

    Candidates are published skills of a type that is not excluded and that
    declare triggers, so entities and integrations never compete with the
    procedures. The index is rebuilt whenever the catalog changes on disk.
    """

    def __init__(
        self,
        registry: SkillRegistry,
        index: SkillIndex,
        config: DiscoveryConfig,
    ) -> None:
        self._registry = registry
        self._index = index
        self._config = config
        self._fingerprint: str | None = None

    def candidates(self) -> list[Skill]:
        """The skills discovery is allowed to choose from."""
        excluded = {str(value) for value in self._config.exclude_types}
        return [
            skill
            for skill in self._registry.published()
            if skill.type.value not in excluded and skill.frontmatter.triggers
        ]

    def select(self, query: str) -> DiscoveryResult:
        """Rank the candidates; choose the top one when it is convincing enough."""
        self._refresh()
        ranked = self._index.rank(query, self._config.limit)
        if not ranked or ranked[0].score < self._config.min_score:
            return DiscoveryResult(ranked=ranked, chosen=None, tie=False)
        tie = (
            len(ranked) > 1
            and ranked[1].score >= self._config.min_score
            and (ranked[0].score - ranked[1].score) < self._config.tie_margin
        )
        return DiscoveryResult(ranked=ranked, chosen=ranked[0].skill_id, tie=tie)

    def _refresh(self) -> None:
        fingerprint = self._registry.fingerprint()
        if fingerprint == self._fingerprint:
            return
        self._index.build(self.candidates())
        self._fingerprint = fingerprint
