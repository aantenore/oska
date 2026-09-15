"""The index protocol and the text handling every index implementation shares.

A skill is indexed as several texts: its description and each of its triggers,
separately. The score of a skill against a request is the best score of its
texts, and the text that produced it is reported as `matched_on`.

Three kinds of text can stand behind a match, and a ranking says which. What an
author declared comes first and is the only authored truth. The phrases derived
from those triggers at publish time follow, carrying the language they were
written for, which is how a catalog written in one language is reached in
another. The phrasings a scheduled job kept from traffic that reached a
procedure come last; they are read from the state directory and never from a
skill directory, so deleting one file undoes them.

The three share one cap per route, and the cap never cuts a declared text: what
gives way is a derived or a learned phrase. Because a skill scores the best of
its texts rather than their average, more phrases can raise a score and never
dilute one.

Which texts an index is built over is decided by whoever builds it, so the same
index code serves a catalog with derived and learned phrases and one without.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import NamedTuple, Protocol, runtime_checkable

from opsdeck.catalog.enrichment import read_examples
from opsdeck.config import EnrichmentConfig
from opsdeck.contracts.common import RankedSkill
from opsdeck.contracts.skill import Skill

__all__ = [
    "DECLARED",
    "DERIVED",
    "LEARNED",
    "LEARNED_FILENAME",
    "IndexedText",
    "LearnedPhrases",
    "SkillIndex",
    "SkillText",
    "TextSource",
    "enriched_source",
    "indexed_texts",
    "learned_path",
    "normalise",
    "overlap_score",
    "routing_source",
    "textualise",
    "tokenise",
    "top_ranked",
]

DECLARED = "declared"
DERIVED = "derived"
LEARNED = "learned"
LEARNED_FILENAME = "learned-examples.json"
PHRASES_KEY = "phrases"

_PUNCTUATION = re.compile(r"[^\w\s]", re.UNICODE)
_WHITESPACE = re.compile(r"\s+")


class SkillText(NamedTuple):
    """One text a skill is indexed under, and where the text came from."""

    text: str
    language: str = ""
    origin: str = DECLARED


class IndexedText(NamedTuple):
    """One text belonging to one skill."""

    skill_id: str
    text: str
    language: str = ""
    origin: str = DECLARED

    @property
    def label(self) -> str:
        """What `matched_on` reports, naming anything that is not declared.

        A derived phrase is named with the language it was derived for, and a
        learned one is named as learned, so a surprising route says which of the
        three kinds of phrase won it.
        """
        if self.origin == LEARNED:
            named = f"learned {self.language}" if self.language else "learned"
            return f"{named}: {self.text}"
        return f"{self.language}: {self.text}" if self.language else self.text


TextSource = Callable[[Skill], list[SkillText]]


@runtime_checkable
class SkillIndex(Protocol):
    """Ranks a set of skills against a request."""

    def build(self, skills: list[Skill], texts: TextSource | None = None) -> None:
        """Index the given skills, replacing anything indexed before.

        `texts` decides what each skill is indexed under; without one, only what
        the skill declares is indexed.
        """
        ...

    def rank(self, query: str, limit: int) -> list[RankedSkill]:
        """Return at most `limit` skills, best first."""
        ...


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


def learned_path(learned_root: Path, skill_id: str) -> Path:
    """Where the learned phrasings of one procedure are kept, under the state directory."""
    name = Path(skill_id).name
    if not name or name != skill_id:
        raise KeyError(f"unusable skill id: {skill_id!r}")
    return Path(learned_root) / name / LEARNED_FILENAME


class LearnedPhrases:
    """The phrasings kept beside a procedure, as an index reads them.

    The file is written by the scheduled job that learns it and is read here,
    never written here: a turn that ranks the catalog reads what is already
    there or nothing. It lives under the state directory rather than in a
    catalog root, so an authored skill stays as its author wrote it and one
    deletion undoes everything that was learned for a procedure.
    """

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        """The directory the learned files are read from."""
        return self._root

    def path(self, skill_id: str) -> Path:
        """Where the learned phrasings of one procedure are kept."""
        return learned_path(self._root, skill_id)

    def phrases(self, skill: Skill) -> list[SkillText]:
        """The phrasings learned for this skill, or nothing when there are none."""
        try:
            document = json.loads(self.path(skill.id).read_text(encoding="utf-8"))
        except (KeyError, OSError, ValueError):
            return []
        if not isinstance(document, dict):
            return []
        entries = document.get(PHRASES_KEY)
        if not isinstance(entries, list):
            return []
        kept: list[SkillText] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            text = " ".join(str(entry.get("text", "")).split())
            language = str(entry.get("language", "") or "")
            if text:
                kept.append(SkillText(text, language, LEARNED))
        return kept

    def fingerprint(self, skills: Sequence[Skill]) -> str:
        """Fingerprint of the learned files of these skills, as they are on disk.

        The registry fingerprints the skill files alone, so this is what tells
        an index that a learned file was written, rewritten or reverted.
        """
        digest = hashlib.sha256()
        for skill in skills:
            try:
                path = self.path(skill.id)
                stat = path.stat()
            except (KeyError, OSError):
                continue
            digest.update(f"{path}:{stat.st_size}:{stat.st_mtime_ns}\n".encode())
        return digest.hexdigest()


def textualise(
    skill: Skill,
    derived: Mapping[str, Sequence[str]] | None = None,
    cap: int = 0,
    learned: Sequence[SkillText] = (),
) -> list[SkillText]:
    """The texts a skill is indexed under: declared, then derived, then learned.

    Repeats are dropped, whatever their language or origin. A positive `cap`
    bounds how many texts one skill contributes, but never drops a declared one:
    what a person wrote is the truth, and the phrases beside it give way.
    """
    texts: list[SkillText] = []
    seen: set[str] = set()

    def add(text: str, language: str = "", origin: str = DECLARED) -> None:
        key = normalise(text)
        if not key or key in seen:
            return
        seen.add(key)
        texts.append(SkillText(text, language, origin))

    frontmatter = skill.frontmatter
    if frontmatter.description:
        add(frontmatter.description)
    for trigger in frontmatter.triggers:
        add(trigger)
    declared = len(texts)
    for language in sorted(derived or {}):
        for phrase in (derived or {})[language]:
            add(phrase, language, DERIVED)
    for entry in learned:
        add(entry.text, entry.language, LEARNED)
    if cap > 0:
        return texts[: max(cap, declared)]
    return texts


def routing_source(
    derived: EnrichmentConfig | None = None,
    learned: LearnedPhrases | None = None,
    cap: int = 0,
) -> TextSource:
    """A text source that adds to the declared texts what is written beside them.

    Both files are read, never written: deriving phrases is publish-time work
    and learning them is the work of a scheduled job, so a turn that ranks the
    catalog reads what is already there or nothing.
    """

    def source(skill: Skill) -> list[SkillText]:
        phrases = read_examples(skill, derived) if derived is not None else None
        kept = learned.phrases(skill) if learned is not None else ()
        return textualise(skill, phrases, cap, kept)

    return source


def enriched_source(config: EnrichmentConfig) -> TextSource:
    """A text source that adds the phrases derived beside each skill."""
    return routing_source(config, None, config.cap_per_route)


def indexed_texts(
    skills: Sequence[Skill],
    texts: TextSource | None = None,
) -> list[IndexedText]:
    """Flatten skills into the texts they are indexed under."""
    source = texts or textualise
    return [
        IndexedText(skill.id, entry.text, entry.language, entry.origin)
        for skill in skills
        for entry in source(skill)
    ]


def top_ranked(ranked: Iterable[RankedSkill], limit: int) -> list[RankedSkill]:
    """Drop the skills that matched nothing, order the rest and cut to `limit`."""
    scored = [item for item in ranked if item.score > 0.0]
    scored.sort(key=lambda item: (-item.score, item.skill_id))
    return scored[: max(limit, 0)]
