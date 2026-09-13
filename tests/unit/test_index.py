"""Ranking the catalog against a request, and choosing from the ranking."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path

from opsdeck.catalog.registry import SkillRegistry
from opsdeck.config import DiscoveryConfig
from opsdeck.contracts.common import Message
from opsdeck.index.base import indexed_texts, normalise, tokenise
from opsdeck.index.discovery import Discovery
from opsdeck.index.embedding import EmbeddingIndex
from opsdeck.index.keyword import KeywordIndex

VECTOR_SIZE = 4096


class BagProvider:
    """Deterministic vectors built from the words of a text.

    Every word lands in one slot chosen by its hash, so the cosine of two
    vectors follows the words the two texts share. That makes the embedding
    index testable without a network call and without a stored fixture.
    """

    name = "bag"

    def __init__(self) -> None:
        self.calls = 0

    def complete_json(
        self,
        system: str,
        messages: list[Message],
        schema: dict,
        *,
        temperature: float = 0.0,
    ) -> dict:
        raise NotImplementedError("this provider only embeds")

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return [self._vector(text) for text in texts]

    @staticmethod
    def _vector(text: str) -> list[float]:
        vector = [0.0] * VECTOR_SIZE
        for word in normalise(text).split():
            digest = hashlib.sha256(word.encode()).digest()
            vector[int.from_bytes(digest[:4], "big") % VECTOR_SIZE] += 1.0
        return vector


def _discovery(registry: SkillRegistry, **overrides: object) -> Discovery:
    return Discovery(registry, KeywordIndex(), DiscoveryConfig(**overrides))


def test_a_skill_is_indexed_under_its_description_and_each_trigger(
    registry: SkillRegistry,
) -> None:
    restart = registry.require("runbook.service_restart")
    texts = [entry.text for entry in indexed_texts([restart])]
    assert texts[0] == restart.frontmatter.description
    assert texts[1:] == restart.frontmatter.triggers


def test_keyword_ranking_reports_the_text_that_won(registry: SkillRegistry) -> None:
    index = KeywordIndex()
    index.build(registry.published())
    ranked = index.rank("restart the checkout service", 5)
    assert ranked[0].skill_id == "runbook.service_restart"
    assert ranked[0].matched_on == "restart the checkout service"
    assert ranked[0].score == 1.0


def test_keyword_ranking_is_ordered_and_cut_to_the_limit(registry: SkillRegistry) -> None:
    index = KeywordIndex()
    index.build(registry.published())
    ranked = index.rank("restart the checkout service", 2)
    assert len(ranked) == 2
    assert ranked[0].score >= ranked[1].score


def test_an_unrelated_request_ranks_nothing(registry: SkillRegistry) -> None:
    index = KeywordIndex()
    index.build(registry.published())
    assert index.rank("zzzz qqqq wwww", 5) == []


def test_discovery_leaves_out_composites_entities_and_integrations(
    registry: SkillRegistry,
) -> None:
    discovery = _discovery(registry)
    ids = {skill.id for skill in discovery.candidates()}
    assert "composite.incident_triage" not in ids
    assert not any(id_.startswith(("entity.", "integration.")) for id_ in ids)
    assert "runbook.service_restart" in ids


def test_discovery_includes_composites_when_they_are_not_excluded(
    registry: SkillRegistry,
) -> None:
    discovery = _discovery(registry, exclude_types=[])
    assert "composite.incident_triage" in {skill.id for skill in discovery.candidates()}


def test_discovery_chooses_the_top_ranked_procedure(registry: SkillRegistry) -> None:
    result = _discovery(registry).select("riavvia il servizio")
    assert result.chosen == "runbook.service_restart"
    assert result.tie is False


def test_discovery_chooses_nothing_below_the_minimum_score(registry: SkillRegistry) -> None:
    result = _discovery(registry, min_score=0.9).select("bounce the api please")
    assert result.chosen is None
    assert result.ranked


def test_discovery_reports_a_tie_when_the_top_two_are_close(registry: SkillRegistry) -> None:
    clear = _discovery(registry).select("search the logs for timeout")
    assert clear.tie is False
    wide = _discovery(registry, tie_margin=1.0).select("search the logs for timeout")
    assert wide.tie is True
    assert wide.chosen == clear.chosen


def test_a_tie_needs_a_second_candidate_above_the_minimum(registry: SkillRegistry) -> None:
    result = _discovery(registry, tie_margin=1.0, min_score=0.99).select(
        "search the logs for timeout"
    )
    assert result.chosen == "runbook.log_search"
    assert result.tie is False


def test_discovery_rebuilds_when_the_catalog_changes(
    catalog_copy: Path,
    write_skill: Callable[..., Path],
    runbook_frontmatter: Callable[..., dict],
) -> None:
    registry = SkillRegistry([catalog_copy])
    discovery = _discovery(registry)
    assert discovery.select("drain stuck queue backlog").chosen is None
    write_skill(
        runbook_frontmatter(
            id="runbook.queue_drain",
            description="Drains the queue of stuck jobs, one batch at a time, until it is empty.",
            triggers=[
                "drain the queue of stuck jobs",
                "empty the stuck queue",
                "clear the backlog of jobs",
            ],
        ),
        root=catalog_copy,
    )
    assert discovery.select("drain stuck queue backlog").chosen == "runbook.queue_drain"


def test_the_embedding_index_embeds_once_per_build(registry: SkillRegistry) -> None:
    provider = BagProvider()
    index = EmbeddingIndex(provider)
    index.build(registry.published())
    assert provider.calls == 1
    index.rank("restart the checkout service", 5)
    assert provider.calls == 2


def test_the_embedding_index_ranks_by_shared_words(registry: SkillRegistry) -> None:
    index = EmbeddingIndex(BagProvider())
    index.build(registry.published())
    ranked = index.rank("restart the checkout service", 5)
    assert ranked[0].skill_id == "runbook.service_restart"
    assert ranked[0].matched_on == "restart the checkout service"
    assert ranked[0].score > 0.99


def test_the_embedding_index_keeps_an_unrelated_request_far_from_the_catalog(
    registry: SkillRegistry,
) -> None:
    index = EmbeddingIndex(BagProvider())
    index.build(registry.published())
    ranked = index.rank("zzzz qqqq wwww", 5)
    assert all(item.score < 0.3 for item in ranked)


def test_discovery_agrees_over_either_index(registry: SkillRegistry) -> None:
    keyword = Discovery(registry, KeywordIndex(), DiscoveryConfig())
    embedding = Discovery(registry, EmbeddingIndex(BagProvider()), DiscoveryConfig())
    query = "roll back the checkout service"
    assert keyword.select(query).chosen == embedding.select(query).chosen


def test_normalisation_drops_punctuation_and_case() -> None:
    assert normalise("  Restart, the API!  ") == "restart the api"
    assert tokenise("Restart the API!") == {"restart", "the", "api"}
