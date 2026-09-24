"""Requests that reached nothing, grouped, and described in declared words.

One request nobody could answer is noise. The same request twenty times is a
missing capability, and the only honest way to tell them apart is to count.
A cluster is kept when it is large enough to be a pattern rather than a bad
afternoon.

What a gap suggests comes from the ontology and from nowhere else: the entities
that are already declared and the operations already bound to them. A gap never
suggests a written procedure. It says which words in the catalog the requests
are near, so a person can decide whether something is missing and what it would
be built from.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel, Field

from oska.config import ImprovementConfig
from oska.improvement.learned import Similarity
from oska.improvement.signals import TurnSignal
from oska.index.base import normalize, overlap_score, tokenize
from oska.ontology.graph import OntologyGraph
from oska.ontology.queries import operations_on, vocabulary
from oska.planner.provider import ModelProvider

__all__ = ["MAX_SAMPLES", "MAX_SUGGESTIONS", "Gap", "find_gaps", "unmatched"]

MAX_SAMPLES = 3
MAX_SUGGESTIONS = 3


class Gap(BaseModel):
    """A group of requests no procedure answered, and what the ontology has near them."""

    size: int = 0
    sample_phrases: list[str] = Field(default_factory=list)
    suggested_entities: list[str] = Field(default_factory=list)
    suggested_operations: list[str] = Field(default_factory=list)


def unmatched(signals: Sequence[TurnSignal], min_score: float) -> list[TurnSignal]:
    """The turns that reached no procedure: nothing chosen, or nothing above the floor.

    A request routed off scope leaves no chosen procedure, so it lands here
    beside the ones discovery declined.
    """
    return [
        signal
        for signal in signals
        if signal.text.strip() and (signal.chosen_runbook is None or signal.score < min_score)
    ]


def find_gaps(
    signals: Sequence[TurnSignal],
    graph: OntologyGraph,
    config: ImprovementConfig,
    *,
    min_score: float,
    provider: ModelProvider | None = None,
) -> list[Gap]:
    """Cluster the requests that reached nothing, and describe the large clusters."""
    phrases = [normalize(signal.text) for signal in unmatched(signals, min_score)]
    phrases = [phrase for phrase in phrases if phrase]
    similarity = Similarity(provider)
    similarity.prime(phrases)
    clusters = _cluster(phrases, similarity, config.cluster_threshold)
    kept = [cluster for cluster in clusters if len(cluster) >= config.cluster_min_size]
    kept.sort(key=lambda cluster: (-len(cluster), cluster[0]))
    return [_describe(cluster, graph) for cluster in kept]


def _cluster(
    phrases: Sequence[str],
    similarity: Similarity,
    threshold: float,
) -> list[list[str]]:
    """Greedy grouping: a phrase joins the first cluster it is close enough to."""
    clusters: list[list[str]] = []
    for phrase in phrases:
        for cluster in clusters:
            if similarity.closest(phrase, cluster) >= threshold:
                cluster.append(phrase)
                break
        else:
            clusters.append([phrase])
    return clusters


def _describe(cluster: Sequence[str], graph: OntologyGraph) -> Gap:
    """One cluster, with the declared entities and operations its words are nearest."""
    words = {token for phrase in cluster for token in tokenize(phrase)}
    entities = _nearest_entities(words, graph)
    operations = [
        str(node.label or node.id)
        for entity_id in entities
        for node in operations_on(graph, entity_id)
    ]
    return Gap(
        size=len(cluster),
        sample_phrases=list(dict.fromkeys(cluster))[:MAX_SAMPLES],
        suggested_entities=entities,
        suggested_operations=list(dict.fromkeys(operations))[:MAX_SUGGESTIONS],
    )


def _nearest_entities(words: set[str], graph: OntologyGraph) -> list[str]:
    """The declared entities whose own words the cluster shares most."""
    scored: list[tuple[float, str]] = []
    for entity_id, entry in vocabulary(graph).items():
        score = overlap_score(words, _entity_words(entity_id, entry))
        if score > 0.0:
            scored.append((score, entity_id))
    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    return [entity_id for _, entity_id in scored[:MAX_SUGGESTIONS]]


def _entity_words(entity_id: str, entry: dict) -> set[str]:
    """Everything an entity is spoken about with, as a set of words."""
    parts = [entity_id.split(".", 1)[-1], str(entry.get("description", ""))]
    parts.extend(str(name) for name in entry.get("fields", []))
    parts.extend(str(state) for state in entry.get("states", []))
    identifier = entry.get("identifier")
    if identifier:
        parts.append(str(identifier))
    return tokenize(" ".join(parts))
