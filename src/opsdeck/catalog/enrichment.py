"""Routing examples derived from the declared triggers, written at publish time.

An author writes the trigger examples of a procedure once, in the language they
work in. The catalog is reached in more languages than that. Translating every
trigger by hand does not scale and rots as soon as a trigger is edited, and
asking a model at turn time would spend a call on every turn. So the paraphrases
are produced once, when a procedure is published or when a scheduled job asks
for them, and written to a file beside the skill that the index reads alongside
the declared examples.

The declared triggers stay the only authored truth. The derived file records the
hash of the triggers it was made from; the moment that hash stops matching, the
file is ignored rather than repaired, so a stale paraphrase is never used and
the fix is always to regenerate. Nothing here is edited by hand.

The file is kept inside the skill's version directory so that it travels with
the version it describes, is copied with it and is removed with it. Reading
`catalog/registry.py` shows what that does not buy: the registry fingerprints
only the `skill.md` files `discover_all` finds, by path, size and modification
time, so writing this file beside one of them leaves the registry fingerprint
unchanged and does not by itself force an index rebuild. `examples_fingerprint`
is what closes that gap, and the index folds it into the fingerprint it holds.

Nothing in this module runs during a turn.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from opsdeck.catalog.registry import SkillRegistry
from opsdeck.config import EnrichmentConfig
from opsdeck.contracts.common import Message
from opsdeck.contracts.skill import Skill

__all__ = [
    "EnrichedExamples",
    "EnrichmentReport",
    "PhraseProvider",
    "enrich",
    "enrich_catalog",
    "examples_fingerprint",
    "examples_path",
    "needs_enrichment",
    "read_examples",
    "source_hash",
    "write_examples",
]

EnrichedExamples = dict[str, list[str]]

SOURCE_HASH_KEY = "source_hash"
LANGUAGES_KEY = "languages"

_SYSTEM = (
    "You write alternative wordings for one procedure of a catalog.\n\n"
    "You are given the description of the procedure and the phrases its author "
    "wrote down as examples of how somebody asks for it. Write further phrases "
    "that a person could use to ask for the same thing, one set per language.\n\n"
    "Rules:\n"
    "- Keep the meaning of the declared phrases exactly, and add nothing they "
    "do not already say.\n"
    "- Write the way a person asks for something, not the way a document "
    "describes it.\n"
    "- Give the requested number of phrases for each language, and no phrase "
    "twice.\n"
    "- Answer with one array of phrases per language, keyed by its code."
)


@runtime_checkable
class PhraseProvider(Protocol):
    """The one call this module needs from a model backend.

    The whole provider interface belongs to the cognition layer, which sits
    above the catalog; naming only the part used here keeps the dependency
    pointing downwards, and any provider satisfies it structurally.
    """

    def complete_json(
        self,
        system: str,
        messages: list[Message],
        schema: dict,
        *,
        temperature: float = 0.0,
    ) -> dict: ...


@dataclass(frozen=True)
class EnrichmentReport:
    """What one pass over a catalog did, by skill identifier."""

    written: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict:
        """The report as plain data, for a channel that prints JSON."""
        return {
            "written": list(self.written),
            "skipped": list(self.skipped),
            "failed": dict(self.failed),
        }


def source_hash(skill: Skill) -> str:
    """Hash of the declared triggers this skill's derived phrases must match."""
    joined = "\n".join(skill.frontmatter.triggers)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def examples_path(skill: Skill, config: EnrichmentConfig) -> Path:
    """Where the derived phrases of this skill are kept: beside the skill file."""
    return Path(skill.path).parent / config.filename


def read_examples(skill: Skill, config: EnrichmentConfig) -> EnrichedExamples:
    """The derived phrases of a skill, or nothing when the file is absent or stale.

    A file written from triggers that have since been edited describes a
    procedure that no longer exists, so it is ignored rather than used.
    """
    path = examples_path(skill, config)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(document, dict):
        return {}
    if document.get(SOURCE_HASH_KEY) != source_hash(skill):
        return {}
    return _clean(document.get(LANGUAGES_KEY))


def write_examples(
    skill: Skill,
    examples: EnrichedExamples,
    config: EnrichmentConfig,
) -> Path:
    """Write the derived phrases beside the skill, stamped with the trigger hash."""
    path = examples_path(skill, config)
    document = {
        SOURCE_HASH_KEY: source_hash(skill),
        LANGUAGES_KEY: _clean(examples),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def needs_enrichment(
    skill: Skill,
    config: EnrichmentConfig,
    languages: Sequence[str],
) -> bool:
    """Whether this skill is missing derived phrases for any of these languages.

    This asks about the files, not about the configuration: `enabled` says
    whether the runtime consumes and produces them on its own, and a person who
    runs the command is asking for them whatever that flag says.
    """
    if not skill.frontmatter.triggers:
        return False
    held = read_examples(skill, config)
    return any(not held.get(language) for language in _wanted(languages))


def enrich(
    skill: Skill,
    provider: PhraseProvider,
    config: EnrichmentConfig,
    languages: Sequence[str],
) -> EnrichedExamples:
    """Paraphrase the declared triggers into every language, in one call."""
    wanted = _wanted(languages)
    if not wanted or not skill.frontmatter.triggers:
        return {}
    answer = provider.complete_json(_SYSTEM, [_request(skill, config, wanted)], _schema(wanted))
    declared = {_key(trigger) for trigger in skill.frontmatter.triggers}
    produced: EnrichedExamples = {}
    for language in wanted:
        phrases = _phrases(answer.get(language), declared, config.per_language)
        if phrases:
            produced[language] = phrases
    return produced


def enrich_catalog(
    registry: SkillRegistry,
    provider: PhraseProvider,
    config: EnrichmentConfig,
    languages: Sequence[str],
) -> EnrichmentReport:
    """Enrich every published skill that declares triggers and lacks a language.

    One skill that fails is recorded and the pass continues, because a catalog
    half enriched is still better routed than one not enriched at all.
    """
    report = EnrichmentReport()
    for skill in registry.published():
        if not skill.frontmatter.triggers:
            continue
        if not needs_enrichment(skill, config, languages):
            report.skipped.append(skill.id)
            continue
        try:
            examples = enrich(skill, provider, config, languages)
            if not examples:
                report.failed[skill.id] = "the provider returned no usable phrase"
                continue
            write_examples(skill, examples, config)
        except Exception as exc:  # one failure must not end the pass
            report.failed[skill.id] = f"{type(exc).__name__}: {exc}"
            continue
        report.written.append(skill.id)
    return report


def examples_fingerprint(skills: Sequence[Skill], config: EnrichmentConfig) -> str:
    """Fingerprint of the derived files of these skills, as they are on disk.

    The registry fingerprints the skill files alone, so this is what tells an
    index that a derived file was written, rewritten or removed.
    """
    digest = hashlib.sha256()
    for skill in skills:
        path = examples_path(skill, config)
        try:
            stat = path.stat()
        except OSError:
            continue
        digest.update(f"{path}:{stat.st_size}:{stat.st_mtime_ns}\n".encode())
    return digest.hexdigest()


def _request(skill: Skill, config: EnrichmentConfig, languages: list[str]) -> Message:
    brief = {
        "description": skill.frontmatter.description,
        "declared_phrases": list(skill.frontmatter.triggers),
        "languages": languages,
        "phrases_per_language": config.per_language,
    }
    return Message(role="user", content=json.dumps(brief, ensure_ascii=False, sort_keys=True))


def _schema(languages: list[str]) -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(languages),
        "properties": {
            language: {
                "type": "array",
                "items": {"type": "string"},
                "description": f"Phrases in {language}, as a person would ask.",
            }
            for language in languages
        },
    }


def _phrases(value: object, declared: set[str], limit: int) -> list[str]:
    """The usable phrases of one answered language: no repeat, no declared trigger."""
    if not isinstance(value, list):
        return []
    seen = set(declared)
    kept: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        phrase = " ".join(item.split())
        key = _key(phrase)
        if not key or key in seen:
            continue
        seen.add(key)
        kept.append(phrase)
        if len(kept) >= max(limit, 0):
            break
    return kept


def _clean(value: object) -> EnrichedExamples:
    """The mapping, keeping only languages that name a list of non-empty phrases."""
    if not isinstance(value, dict):
        return {}
    cleaned: EnrichedExamples = {}
    for language, phrases in value.items():
        if not isinstance(language, str) or not isinstance(phrases, list):
            continue
        kept = [
            " ".join(item.split())
            for item in phrases
            if isinstance(item, str) and item.strip()
        ]
        if kept:
            cleaned[language] = kept
    return cleaned


def _wanted(languages: Sequence[str]) -> list[str]:
    """The languages asked for, trimmed, in order, without repeats."""
    seen: dict[str, None] = {}
    for language in languages:
        name = str(language).strip()
        if name:
            seen.setdefault(name, None)
    return list(seen)


def _key(text: str) -> str:
    return " ".join(text.lower().split())
