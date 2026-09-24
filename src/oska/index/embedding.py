"""Ranking by cosine similarity over vectors obtained from a model provider."""

from __future__ import annotations

import math

from oska.contracts.common import RankedSkill
from oska.contracts.skill import Skill
from oska.index.base import IndexedText, TextSource, indexed_texts, top_ranked
from oska.planner.provider import ModelProvider

__all__ = ["EmbeddingIndex"]


class EmbeddingIndex:
    """Embeds the catalog once per build and compares the request against it.

    Vectors are held in memory for the life of the index; a rebuild recomputes
    them. One call embeds every indexed text, one call embeds the request.
    """

    def __init__(self, provider: ModelProvider) -> None:
        self._provider = provider
        self._entries: list[IndexedText] = []
        self._vectors: list[list[float]] = []

    def build(self, skills: list[Skill], texts: TextSource | None = None) -> None:
        """Embed every text the given source indexes each skill under."""
        self._entries = indexed_texts(skills, texts)
        self._vectors = []
        if not self._entries:
            return
        vectors = self._provider.embed([entry.text for entry in self._entries])
        if len(vectors) != len(self._entries):
            raise ValueError(
                f"the provider returned {len(vectors)} vectors for {len(self._entries)} texts"
            )
        self._vectors = [_unit(vector) for vector in vectors]

    def rank(self, query: str, limit: int) -> list[RankedSkill]:
        """Return the skills whose texts sit closest to the request."""
        if not self._entries or not query.strip():
            return []
        embedded = self._provider.embed([query])
        if not embedded:
            return []
        query_vector = _unit(embedded[0])
        best: dict[str, RankedSkill] = {}
        for entry, vector in zip(self._entries, self._vectors, strict=True):
            score = _cosine(query_vector, vector)
            held = best.get(entry.skill_id)
            if held is None or score > held.score:
                best[entry.skill_id] = RankedSkill(
                    skill_id=entry.skill_id, score=score, matched_on=entry.label
                )
        return top_ranked(best.values(), limit)


def _unit(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0.0:
        return [0.0] * len(vector)
    return [value / norm for value in vector]


def _cosine(left: list[float], right: list[float]) -> float:
    """Cosine of two unit vectors, floored at zero so scores stay in 0..1."""
    product = sum(a * b for a, b in zip(left, right, strict=False))
    return max(0.0, min(1.0, product))
