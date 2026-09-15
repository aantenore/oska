"""Choosing which procedure a request is about."""

from __future__ import annotations

from opsdeck.catalog.enrichment import examples_fingerprint
from opsdeck.catalog.registry import SkillRegistry
from opsdeck.config import DiscoveryConfig, EnrichmentConfig
from opsdeck.contracts.common import DiscoveryResult
from opsdeck.contracts.skill import Skill
from opsdeck.index.base import LearnedPhrases, SkillIndex, TextSource, routing_source

__all__ = ["Discovery"]


class Discovery:
    """Ranks the catalog against a request and says whether the top is clear.

    Candidates are published skills of a type that is not excluded and that
    declare triggers, so entities and integrations never compete with the
    procedures. The index is rebuilt whenever the catalog changes on disk.

    When enrichment is in force, each candidate is also indexed under the
    phrases derived from its triggers at publish time, so a request in a
    language the author did not write in still reaches the procedure. When a
    learned store is given, it is also indexed under the phrasings a scheduled
    job kept from traffic that reached it. Both are read here and written
    nowhere: ranking a request costs no model call that ranking did not already
    cost, and both are folded into the fingerprint the index holds, so writing
    or reverting one of those files rebuilds it.
    """

    def __init__(
        self,
        registry: SkillRegistry,
        index: SkillIndex,
        config: DiscoveryConfig,
        enrichment: EnrichmentConfig | None = None,
        learned: LearnedPhrases | None = None,
    ) -> None:
        self._registry = registry
        self._index = index
        self._config = config
        self._derived = enrichment if enrichment is not None and enrichment.enabled else None
        self._learned = learned
        cap = enrichment.cap_per_route if enrichment is not None else 0
        self._texts: TextSource | None = (
            routing_source(self._derived, learned, cap)
            if self._derived is not None or learned is not None
            else None
        )
        self._fingerprint: str | None = None

    @property
    def texts(self) -> TextSource | None:
        """What each candidate is indexed under, for whoever ranks the same catalog."""
        return self._texts

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
        candidates = self.candidates()
        fingerprint = self._registry.fingerprint()
        # The registry fingerprints the skill files alone, so a derived file
        # written beside one of them, or a learned file written under the state
        # directory, would otherwise go unnoticed.
        if self._derived is not None:
            fingerprint = f"{fingerprint}:{examples_fingerprint(candidates, self._derived)}"
        if self._learned is not None:
            fingerprint = f"{fingerprint}:{self._learned.fingerprint(candidates)}"
        if fingerprint == self._fingerprint:
            return
        self._index.build(candidates, self._texts)
        self._fingerprint = fingerprint
