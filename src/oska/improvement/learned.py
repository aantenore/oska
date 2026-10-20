"""The words people really used, kept beside a procedure and never inside it.

A procedure declares its triggers: the wording its author thought of. Traffic
shows the wording people actually reach for. The second is worth keeping, and
it is worth keeping apart.

Learned phrasings are runtime state, not authored truth. They live in one file
per procedure under the improvement root, which is under the state directory and
never inside a skill directory, and they carry the moment they were written. An
authored skill is never edited: an edit cannot be undone by deleting anything,
and the author would be left maintaining sentences nobody wrote.

What is written here is what the index reads. A procedure is ranked under its
declared triggers, the phrases derived from them at publish time, and then
these, under one cap per route; the ranking says which of the three it matched
on. `revert` deletes the file, the fingerprint the index holds changes with it,
and the next ranking is the one that was there before.

Three filters stand between a request and a learned phrase. It has to have come
from a turn that reached a procedure and finished. It has to say something the
declared triggers and the phrases already kept do not already say. And anything
that looks like an identifier, a reference or an address is replaced, using
patterns read from a file rather than written into this module.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

from oska.catalog.registry import Catalog
from oska.config import ImprovementConfig
from oska.improvement.signals import TurnSignal
from oska.index.base import (
    LEARNED_FILENAME,
    learned_path,
    normalize,
    overlap_score,
    tokenize,
)
from oska.planner.provider import ModelProvider, provider_embeds

__all__ = [
    "DEFAULT_REDACTIONS",
    "LEARNED_FILENAME",
    "NEAR_DUPLICATE",
    "LearnedPhrase",
    "Redaction",
    "Similarity",
    "learn",
    "learned_path",
    "load_redactions",
    "read_learned",
    "redact",
    "revert",
    "write_learned",
]

DEFAULT_REDACTIONS = Path("examples/ops/evaluation/redactions.json")
NEAR_DUPLICATE = 0.7
MIN_WORDS = 2


class Redaction(BaseModel):
    """One declared pattern and what replaces it."""

    name: str
    pattern: str
    replacement: str


class LearnedPhrase(BaseModel):
    """One request that reached a procedure, in the words it was asked in."""

    text: str
    language: str = ""
    session_id: str = ""


class Similarity:
    """How close two phrasings are.

    With a provider, the cosine of their embeddings; without one, the token
    overlap the keyword index already ranks by. `prime` embeds a whole batch in
    one call, so a run costs one request per set of texts and not one per
    comparison; a text that was not primed falls back to token overlap.
    """

    def __init__(self, provider: ModelProvider | None = None) -> None:
        self._provider = provider
        self._vectors: dict[str, list[float]] = {}

    def prime(self, texts: Sequence[str]) -> None:
        """Embed these texts once, if there is a provider to embed them with."""
        if self._provider is None or not provider_embeds(self._provider):
            return
        wanted = [text for text in dict.fromkeys(texts) if text and text not in self._vectors]
        if not wanted:
            return
        vectors = self._provider.embed(list(wanted))
        for text, vector in zip(wanted, vectors, strict=False):
            self._vectors[text] = _unit(vector)

    def between(self, left: str, right: str) -> float:
        """Closeness of two texts, between 0 and 1."""
        first = self._vectors.get(left)
        second = self._vectors.get(right)
        if first is not None and second is not None:
            return _cosine(first, second)
        return overlap_score(tokenize(left), tokenize(right))

    def closest(self, text: str, others: Iterable[str]) -> float:
        """The closeness of the nearest of `others`, or zero when there are none."""
        scores = [self.between(text, other) for other in others]
        return max(scores) if scores else 0.0


def load_redactions(path: Path | None = None) -> list[Redaction]:
    """Read the declared redaction patterns. A missing file means none are declared."""
    target = Path(path) if path is not None else DEFAULT_REDACTIONS
    if not target.is_file():
        return []
    document = json.loads(target.read_text(encoding="utf-8"))
    declared = document.get("patterns", []) if isinstance(document, dict) else document
    return [Redaction.model_validate(entry) for entry in declared]


def redact(text: str, redactions: Sequence[Redaction]) -> str:
    """Replace everything the declared patterns match."""
    result = text
    for redaction in redactions:
        result = re.sub(redaction.pattern, redaction.replacement, result, flags=re.IGNORECASE)
    return result


def learn(
    signals: Sequence[TurnSignal],
    registry: Catalog,
    config: ImprovementConfig,
    *,
    provider: ModelProvider | None = None,
    redactions_path: Path | None = None,
    threshold: float = NEAR_DUPLICATE,
) -> dict[str, list[LearnedPhrase]]:
    """The phrasings worth keeping, per procedure, from the turns that finished."""
    redactions = load_redactions(redactions_path)
    by_skill: dict[str, list[TurnSignal]] = {}
    for signal in signals:
        if signal.grounded and signal.chosen_runbook:
            by_skill.setdefault(signal.chosen_runbook, []).append(signal)
    learned: dict[str, list[LearnedPhrase]] = {}
    for skill_id, reached in sorted(by_skill.items()):
        skill = registry.get(skill_id)
        declared = list(skill.frontmatter.triggers) if skill is not None else []
        phrases = _keep(reached, declared, redactions, config, provider, threshold)
        if phrases:
            learned[skill_id] = phrases
    return learned


def write_learned(
    learned_root: Path,
    skill_id: str,
    phrases: Sequence[LearnedPhrase],
    *,
    at: datetime | None = None,
) -> Path:
    """Write the learned phrasings of one procedure, replacing any earlier file."""
    path = learned_path(learned_root, skill_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "learned_at": (at or datetime.now(UTC)).isoformat(),
        "phrases": [phrase.model_dump(mode="json") for phrase in phrases],
    }
    path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def read_learned(learned_root: Path, skill_id: str) -> list[LearnedPhrase]:
    """The learned phrasings held for one procedure, or nothing when there are none."""
    path = learned_path(learned_root, skill_id)
    if not path.is_file():
        return []
    document = json.loads(path.read_text(encoding="utf-8"))
    return [LearnedPhrase.model_validate(entry) for entry in document.get("phrases", [])]


def revert(learned_root: Path, skill_id: str) -> bool:
    """Delete what was learned for one procedure. True when there was something.

    Deleting the file is the whole of the undo. An index that ranks by these
    phrases folds the file into the fingerprint it holds, so the next ranking
    after the deletion is built without them.
    """
    path = learned_path(learned_root, skill_id)
    if not path.is_file():
        return False
    path.unlink()
    parent = path.parent
    if parent.is_dir() and not any(parent.iterdir()):
        parent.rmdir()
    return True


def _keep(
    signals: Sequence[TurnSignal],
    declared: Sequence[str],
    redactions: Sequence[Redaction],
    config: ImprovementConfig,
    provider: ModelProvider | None,
    threshold: float,
) -> list[LearnedPhrase]:
    """Normalize, redact, drop what is already said, and cap."""
    candidates: list[tuple[str, TurnSignal]] = []
    for signal in signals:
        text = normalize(redact(signal.text, redactions)).strip()
        if len(text.split()) >= MIN_WORDS:
            candidates.append((text, signal))
    similarity = Similarity(provider)
    similarity.prime([text for text, _ in candidates] + [normalize(item) for item in declared])
    against = [normalize(item) for item in declared]
    kept: list[LearnedPhrase] = []
    for text, signal in candidates:
        if len(kept) >= config.max_phrases_per_skill:
            break
        if similarity.closest(text, against) >= threshold:
            continue
        kept.append(
            LearnedPhrase(text=text, language=signal.language, session_id=signal.session_id)
        )
        against.append(text)
    return kept


def _unit(vector: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0.0:
        return [0.0] * len(vector)
    return [value / norm for value in vector]


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    product = sum(a * b for a, b in zip(left, right, strict=False))
    return max(0.0, min(1.0, product))
