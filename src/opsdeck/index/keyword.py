"""Ranking by word overlap, with no model behind it."""

from __future__ import annotations

from opsdeck.contracts.common import RankedSkill
from opsdeck.contracts.skill import Skill
from opsdeck.index.base import IndexedText, indexed_texts, overlap_score, tokenise, top_ranked

__all__ = ["KeywordIndex"]


class KeywordIndex:
    """Scores each indexed text by the words it shares with the request.

    This is the default when no model provider is configured. It matches the
    wording an author wrote in the triggers, so a request phrased differently
    scores low and discovery declines rather than guessing.
    """

    def __init__(self) -> None:
        self._entries: list[tuple[IndexedText, set[str]]] = []

    def build(self, skills: list[Skill]) -> None:
        """Tokenise the description and the triggers of every skill."""
        self._entries = [(entry, tokenise(entry.text)) for entry in indexed_texts(skills)]

    def rank(self, query: str, limit: int) -> list[RankedSkill]:
        """Return the skills sharing the most words with the request."""
        query_tokens = tokenise(query)
        if not query_tokens or not self._entries:
            return []
        best: dict[str, RankedSkill] = {}
        for entry, entry_tokens in self._entries:
            score = overlap_score(query_tokens, entry_tokens)
            held = best.get(entry.skill_id)
            if held is None or score > held.score:
                best[entry.skill_id] = RankedSkill(
                    skill_id=entry.skill_id, score=score, matched_on=entry.text
                )
        return top_ranked(best.values(), limit)
