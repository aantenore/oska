"""Ranking the catalog against a request, and choosing from the ranking."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path

import pytest

from opsdeck.catalog.enrichment import write_examples
from opsdeck.catalog.registry import SkillRegistry
from opsdeck.config import DiscoveryConfig, EnrichmentConfig, FastPathConfig
from opsdeck.contracts.common import Message
from opsdeck.improvement.learned import LearnedPhrase, write_learned
from opsdeck.index.base import (
    DECLARED,
    DERIVED,
    LEARNED,
    LearnedPhrases,
    indexed_texts,
    normalize,
    textualize,
    tokenize,
)
from opsdeck.index.discovery import Discovery
from opsdeck.index.embedding import EmbeddingIndex
from opsdeck.index.keyword import KeywordIndex
from opsdeck.planner.provider import ModelProviderError
from opsdeck.planner.scripted import ScriptedModelProvider
from opsdeck.router.base import RoutingContext
from opsdeck.router.fast_path import FastPath

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
        for word in normalize(text).split():
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


def test_normalization_drops_punctuation_and_case() -> None:
    assert normalize("  Restart, the API!  ") == "restart the api"
    assert tokenize("Restart the API!") == {"restart", "the", "api"}


ITALIAN_REQUEST = "svuota la coda dei lavori bloccati"
ITALIAN_PHRASES = [
    ITALIAN_REQUEST,
    "togli i lavori fermi dalla coda",
    "libera la coda dei processi in attesa",
]


@pytest.fixture
def enrichment() -> EnrichmentConfig:
    """Enrichment in force, with room for the phrases these tests use."""
    return EnrichmentConfig(enabled=True, per_language=3, cap_per_route=24)


ONE_LANGUAGE_CATALOG = (
    (
        "runbook.queue_drain",
        "Drains the queue of stuck jobs, one batch at a time, until it is empty.",
        ["drain the queue of stuck jobs", "empty the stuck queue", "clear the backlog of jobs"],
    ),
    (
        "runbook.cache_purge",
        "Clears the cached responses of a service so the next read is served fresh.",
        ["purge the cache of a service", "clear the cached responses", "drop the cache"],
    ),
    (
        "runbook.service_restart",
        "Restarts a service after checking its current state, and confirms it came back up.",
        ["restart the checkout service", "bounce the api", "the service is stuck, restart it"],
    ),
)


@pytest.fixture
def one_language_catalog(
    tmp_path: Path,
    write_skill: Callable[..., Path],
    runbook_frontmatter: Callable[..., dict],
) -> SkillRegistry:
    """A catalog whose procedures are described in one language only."""
    for skill_id, description, triggers in ONE_LANGUAGE_CATALOG:
        write_skill(
            runbook_frontmatter(id=skill_id, description=description, triggers=list(triggers))
        )
    return SkillRegistry([tmp_path / "skills"])


def _embedding_discovery(
    registry: SkillRegistry,
    enrichment: EnrichmentConfig | None = None,
) -> Discovery:
    """Discovery over the hash-bag embeddings of the scripted provider."""
    return Discovery(
        registry,
        EmbeddingIndex(ScriptedModelProvider([])),
        DiscoveryConfig(),
        enrichment,
    )


def test_a_derived_phrase_follows_the_declared_texts_and_names_its_language(
    one_language_catalog: SkillRegistry, enrichment: EnrichmentConfig
) -> None:
    skill = one_language_catalog.require("runbook.queue_drain")
    texts = textualize(skill, {"it": ITALIAN_PHRASES})
    assert [entry.language for entry in texts] == ["", "", "", "", "it", "it", "it"]
    assert texts[0].text == skill.frontmatter.description
    assert texts[4].text == ITALIAN_REQUEST


def test_the_cap_gives_way_before_a_declared_text_does(
    one_language_catalog: SkillRegistry,
) -> None:
    skill = one_language_catalog.require("runbook.queue_drain")
    capped = textualize(skill, {"it": ITALIAN_PHRASES}, cap=5)
    assert len(capped) == 5
    assert [entry.language for entry in capped] == ["", "", "", "", "it"]
    tight = textualize(skill, {"it": ITALIAN_PHRASES}, cap=2)
    assert [entry.text for entry in tight] == [entry.text for entry in textualize(skill)]


def test_a_request_in_the_other_language_ranks_only_once_the_phrases_are_there(
    one_language_catalog: SkillRegistry, enrichment: EnrichmentConfig
) -> None:
    bare = _embedding_discovery(one_language_catalog)
    assert bare.select(ITALIAN_REQUEST).chosen is None
    write_examples(
        one_language_catalog.require("runbook.queue_drain"),
        {"it": ITALIAN_PHRASES},
        enrichment,
    )
    enriched = _embedding_discovery(one_language_catalog, enrichment)
    result = enriched.select(ITALIAN_REQUEST)
    assert result.chosen == "runbook.queue_drain"
    assert result.tie is False
    assert result.ranked[0].score > 0.99


def test_matched_on_names_the_derived_phrase_that_won(
    one_language_catalog: SkillRegistry, enrichment: EnrichmentConfig
) -> None:
    write_examples(
        one_language_catalog.require("runbook.queue_drain"),
        {"it": ITALIAN_PHRASES},
        enrichment,
    )
    result = _embedding_discovery(one_language_catalog, enrichment).select(ITALIAN_REQUEST)
    assert result.ranked[0].matched_on == f"it: {ITALIAN_REQUEST}"
    english = _embedding_discovery(one_language_catalog, enrichment).select(
        "drain the queue of stuck jobs"
    )
    assert english.ranked[0].matched_on == "drain the queue of stuck jobs"


def test_the_declared_texts_still_win_where_they_always_did(
    one_language_catalog: SkillRegistry, enrichment: EnrichmentConfig
) -> None:
    """More phrases may raise a score; a maximum over the texts never dilutes one."""
    query = "restart the checkout service"
    bare = _embedding_discovery(one_language_catalog).select(query)
    write_examples(
        one_language_catalog.require("runbook.queue_drain"),
        {"it": ITALIAN_PHRASES},
        enrichment,
    )
    enriched = _embedding_discovery(one_language_catalog, enrichment).select(query)
    assert enriched.chosen == bare.chosen == "runbook.service_restart"
    assert enriched.ranked[0].score >= bare.ranked[0].score


def test_a_turn_over_an_enriched_catalog_makes_no_enrichment_call(
    one_language_catalog: SkillRegistry, enrichment: EnrichmentConfig
) -> None:
    """The phrases were written at publish time; ranking only reads them back."""
    write_examples(
        one_language_catalog.require("runbook.queue_drain"),
        {"it": ITALIAN_PHRASES},
        enrichment,
    )
    provider = ScriptedModelProvider([])
    with pytest.raises(ModelProviderError):
        provider.complete_json("", [Message(role="user", content=ITALIAN_REQUEST)], {})
    discovery = Discovery(
        one_language_catalog, EmbeddingIndex(provider), DiscoveryConfig(), enrichment
    )
    assert discovery.select(ITALIAN_REQUEST).chosen == "runbook.queue_drain"
    assert provider.consumed() == 0


def test_the_fast_path_routes_a_request_in_the_other_language(
    one_language_catalog: SkillRegistry, enrichment: EnrichmentConfig
) -> None:
    write_examples(
        one_language_catalog.require("runbook.queue_drain"),
        {"it": ITALIAN_PHRASES},
        enrichment,
    )
    config = FastPathConfig()
    bare = FastPath(KeywordIndex, one_language_catalog, config)
    assert bare.route(ITALIAN_REQUEST, RoutingContext()) is None
    enriched = FastPath(KeywordIndex, one_language_catalog, config, enrichment)
    decision = enriched.route(ITALIAN_REQUEST, RoutingContext())
    assert decision is not None
    assert decision.runbook_id == "runbook.queue_drain"
    assert decision.source == "fast_path"


def test_the_index_is_rebuilt_when_the_derived_phrases_are_written(
    one_language_catalog: SkillRegistry, enrichment: EnrichmentConfig
) -> None:
    """The registry fingerprints the skill files alone, so the index adds its own."""
    discovery = _embedding_discovery(one_language_catalog, enrichment)
    assert discovery.select(ITALIAN_REQUEST).chosen is None
    write_examples(
        one_language_catalog.require("runbook.queue_drain"),
        {"it": ITALIAN_PHRASES},
        enrichment,
    )
    assert discovery.select(ITALIAN_REQUEST).chosen == "runbook.queue_drain"


# --- phrasings learned from traffic ---------------------------------------

LEARNED_REQUEST = "unwedge pending work items"
LEARNED_PHRASES = (LEARNED_REQUEST, "nothing is getting through, unwedge it")


def learn(root: Path, skill_id: str, *texts: str, language: str = "en") -> LearnedPhrases:
    """Write phrasings under a state root the way the improvement job writes them."""
    write_learned(
        root,
        skill_id,
        [LearnedPhrase(text=text, language=language, session_id="session-1") for text in texts],
    )
    return LearnedPhrases(root)


def _learned_discovery(
    registry: SkillRegistry,
    learned: LearnedPhrases | None = None,
    enrichment: EnrichmentConfig | None = None,
) -> Discovery:
    """Discovery over the word-bag embeddings, with whatever is beside the catalog."""
    return Discovery(
        registry,
        EmbeddingIndex(BagProvider()),
        DiscoveryConfig(),
        enrichment,
        learned,
    )


def test_a_learned_phrase_follows_the_derived_ones_and_says_it_was_learned(
    one_language_catalog: SkillRegistry, tmp_path: Path
) -> None:
    skill = one_language_catalog.require("runbook.queue_drain")
    store = learn(tmp_path / "state" / "learned", skill.id, *LEARNED_PHRASES)

    texts = textualize(skill, {"it": ITALIAN_PHRASES}, 0, store.phrases(skill))

    origins = [entry.origin for entry in texts]
    assert origins == [DECLARED] * 4 + [DERIVED] * 3 + [LEARNED] * 2
    labels = [entry.label for entry in indexed_texts([skill], lambda _: texts)]
    assert labels[0] == skill.frontmatter.description
    assert labels[4] == f"it: {ITALIAN_REQUEST}"
    assert labels[7] == f"learned en: {LEARNED_REQUEST}"


def test_the_cap_drops_a_learned_phrase_before_a_derived_one(
    one_language_catalog: SkillRegistry, tmp_path: Path
) -> None:
    skill = one_language_catalog.require("runbook.queue_drain")
    store = learn(tmp_path / "state" / "learned", skill.id, *LEARNED_PHRASES)

    capped = textualize(skill, {"it": ITALIAN_PHRASES}, 6, store.phrases(skill))
    tight = textualize(skill, {"it": ITALIAN_PHRASES}, 1, store.phrases(skill))

    assert [entry.origin for entry in capped] == [DECLARED] * 4 + [DERIVED] * 2
    assert [entry.text for entry in tight] == [entry.text for entry in textualize(skill)]


def test_a_request_in_the_learned_words_ranks_only_once_they_are_learned(
    one_language_catalog: SkillRegistry, tmp_path: Path
) -> None:
    root = tmp_path / "state" / "learned"
    bare = _learned_discovery(one_language_catalog, LearnedPhrases(root))
    assert bare.select(LEARNED_REQUEST).chosen is None

    store = learn(root, "runbook.queue_drain", *LEARNED_PHRASES)
    result = _learned_discovery(one_language_catalog, store).select(LEARNED_REQUEST)

    assert result.chosen == "runbook.queue_drain"
    assert result.ranked[0].matched_on == f"learned en: {LEARNED_REQUEST}"
    assert not any(
        root.is_relative_to(skill.path.parent)
        for skill in one_language_catalog.published()
    )


def test_the_index_is_rebuilt_when_a_learned_file_is_written(
    one_language_catalog: SkillRegistry, tmp_path: Path
) -> None:
    """The registry fingerprints the skill files alone, so the index adds its own."""
    root = tmp_path / "state" / "learned"
    discovery = _learned_discovery(one_language_catalog, LearnedPhrases(root))
    assert discovery.select(LEARNED_REQUEST).chosen is None

    learn(root, "runbook.queue_drain", *LEARNED_PHRASES)

    assert discovery.select(LEARNED_REQUEST).chosen == "runbook.queue_drain"


def test_the_fast_path_routes_the_words_that_were_learned(
    one_language_catalog: SkillRegistry, tmp_path: Path
) -> None:
    root = tmp_path / "state" / "learned"
    config = FastPathConfig()
    bare = FastPath(KeywordIndex, one_language_catalog, config, None, LearnedPhrases(root))
    assert bare.route(LEARNED_REQUEST, RoutingContext()) is None

    store = learn(root, "runbook.queue_drain", *LEARNED_PHRASES)
    decision = FastPath(KeywordIndex, one_language_catalog, config, None, store).route(
        LEARNED_REQUEST, RoutingContext()
    )

    assert decision is not None
    assert decision.runbook_id == "runbook.queue_drain"
