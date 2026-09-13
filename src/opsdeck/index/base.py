"""The index protocol and the text handling every index implementation shares.

A skill is indexed as several texts: its description and each of its triggers,
separately. The score of a skill against a request is the best score of its
texts, and the text that produced it is reported as `matched_on`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import NamedTuple, Protocol, runtime_checkable

from opsdeck.contracts.common import RankedSkill
from opsdeck.contracts.skill import Skill

__all__ = [
    "IndexedText",
    "SkillIndex",
    "indexed_texts",
    "normalise",
    "overlap_score",
    "textualise",
    "tokenise",
    "top_ranked",
]

_PUNCTUATION = re.compile(r"[^\w\s]", re.UNICODE)
_WHITESPACE = re.compile(r"\s+")


@runtime_checkable
class SkillIndex(Protocol):
    """Ranks a set of skills against a request."""

    def build(self, skills: list[Skill]) -> None:
        """Index the given skills, replacing anything indexed before."""
        ...

    def rank(self, query: str, limit: int) -> list[RankedSkill]:
        """Return at most `limit` skills, best first."""
        ...


class IndexedText(NamedTuple):
    """One text belonging to one skill."""

    skill_id: str
    text: str


def normalise(text: str) -> str:
    """Lowercase the text and reduce punctuation and runs of space to single spaces."""
    return _WHITESPACE.sub(" ", _PUNCTUATION.sub(" ", text.lower())).strip()


def tokenise(text: str) -> set[str]:
    """The distinct words of a text, normalised."""
    normalised = normalise(text)
    return set(normalised.split()) if normalised else set()


def overlap_score(left: set[str], right: set[str]) -> float:
    """Share of the words the two texts have in common, between 0 and 1."""
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def textualise(skill: Skill) -> list[str]:
    """The texts a skill is indexed under: its description, then each trigger."""
    frontmatter = skill.frontmatter
    texts = [frontmatter.description] if frontmatter.description else []
    texts.extend(trigger for trigger in frontmatter.triggers if trigger)
    return texts


def indexed_texts(skills: Sequence[Skill]) -> list[IndexedText]:
    """Flatten skills into the texts they are indexed under."""
    return [IndexedText(skill.id, text) for skill in skills for text in textualise(skill)]


def top_ranked(ranked: Iterable[RankedSkill], limit: int) -> list[RankedSkill]:
    """Drop the skills that matched nothing, order the rest and cut to `limit`."""
    scored = [item for item in ranked if item.score > 0.0]
    scored.sort(key=lambda item: (-item.score, item.skill_id))
    return scored[: max(limit, 0)]
