"""What the runtime says in its own words, in the language of the conversation.

Most of a reply is somebody else's text: the catalog's wording, a model's
rephrasing, a value a connector returned. The rest is the runtime speaking for
itself: the approval it asks for, the line that names a receipt, the sentence
that says a capability is down. Those sentences live in a phrase catalog, a
JSON file keyed by language and then by phrase:

    {"en": {"approval_tail": "Reply {approve} to approve it, or {reject} ..."},
     "it": {"approval_tail": "Rispondi {approve} per approvarla, ..."}}

The set of phrases is fixed here, in `REQUIRED`, because it is the code that
asks for them. The catalog shipped with the package holds every one of them in
English, the reference language, and loading refuses a packaged catalog that
does not. A configured catalog is laid over it phrase by phrase, so it may
translate a single sentence and leave the rest alone. Further layers are laid
the same way after it: on a platform, the `phrases.json` of each pack a domain
serves, in the order the domain serves them. Every layer is held to the same
checks.

A phrase is a template with named placeholders. The values put into it (an
operation name, an integration id, a payload, a receipt id, a reason) are
inserted exactly as they are: a phrase changes the sentence around a value,
never the value. A translation must name exactly the placeholders its English
phrase names, and a catalog where one does not is refused when it is loaded,
not when a turn first needs the sentence.

A phrase asked for in a language the catalog does not hold, or does not hold
that phrase in, is said in the default language of the replies and then in
English: the fallback is per phrase, so a partial translation is still used
for everything it covers.

Two entries are lists rather than templates. `claim_verified` holds the words
that call a write verified and `negations` the phrases that say it is not;
the conformance check for overstated claims reads both, in every language the
catalog holds. They are matched as case-insensitive substrings, so a stem
covers every ending of a word.

`approve_word` and `reject_word` are what an approval request tells a person
to type. Each must be one of the tokens the approval rules accept, compared
the way the rule router compares them, or loading fails: a reply never asks
for an answer the router would not recognize.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from string import Formatter

from oska.config import (
    PACKAGED_CONFIGS,
    PHRASES_PATH,
    RouterRules,
    RuntimeConfig,
    Settings,
    phrases_path,
)
from oska.index.base import normalize

__all__ = [
    "LIST_PHRASES",
    "REFERENCE_LANGUAGE",
    "REQUIRED",
    "PhraseCatalogError",
    "Phrases",
    "load_phrases",
    "read_catalog",
]

REFERENCE_LANGUAGE = "en"
"""The language the packaged catalog holds complete, and the last fallback."""

LIST_PHRASES: tuple[str, ...] = ("claim_verified", "negations")
"""Entries that are lists of words rather than templates."""

REQUIRED: tuple[str, ...] = (
    "greeting",
    "off_scope",
    "no_planner",
    "nothing_to_report",
    "question",
    "approval_head",
    "approval_payload",
    "approval_first_of_two",
    "approval_second_of_two",
    "approval_tail",
    "approve_word",
    "reject_word",
    "approval_pending",
    "read",
    "read_failed",
    "no_reason_given",
    "receipt_verified",
    "declared_read_back",
    "receipt_contradicted",
    "receipt_unconfirmed",
    "receipt_failed",
    "call_did_not_complete",
    "receipt_unknown",
    "already_done",
    "already_asked",
    "unavailable",
    "unavailable_write",
    "unavailable_read",
    "unavailable_unknown",
    "state_unconfirmed",
    "did_not_answer",
    "the_read_before_it",
    "when_now",
    "when_one_second",
    "when_seconds",
    "denied",
    "rejected",
    "rejected_because",
    "invalid_plan",
    "the_procedure",
    "rejected_payload_moved",
    "cancelled",
    "cancelled_dropped",
    "cancelled_by_request",
    "cancelled_over_budget",
    "other_outcome",
    *LIST_PHRASES,
)
"""Every phrase the runtime asks for. The packaged catalog holds each in English."""

_PACKAGED = PACKAGED_CONFIGS / PHRASES_PATH.name

Entry = str | tuple[str, ...]


class PhraseCatalogError(ValueError):
    """A phrase catalog that cannot be used as it stands."""


@dataclass(frozen=True)
class Phrases:
    """A loaded phrase catalog: every language it holds, merged and checked."""

    catalog: Mapping[str, Mapping[str, Entry]]
    default_language: str = REFERENCE_LANGUAGE
    accepted: Mapping[str, frozenset[str]] = field(default_factory=dict)
    """The approval words the router's rules accept, by key; empty when no rules were given."""

    def __post_init__(self) -> None:
        _check(self.catalog)

    @classmethod
    def load(
        cls,
        *paths: Path,
        default_language: str = REFERENCE_LANGUAGE,
        rules: RouterRules | None = None,
        spoken: Sequence[str] = (),
    ) -> Phrases:
        """The packaged catalog with each file laid over it in order, checked.

        With `rules`, the words an approval request tells a person to type are
        checked against the tokens those rules accept, in the default language
        and in every language of `spoken`: the languages the deployment says it
        answers in, not every language the catalog happens to hold.
        """
        layers = [_read(_PACKAGED)]
        layers.extend(_read(Path(path)) for path in paths if Path(path) != _PACKAGED)
        phrases = cls(_merge(layers), default_language=default_language)
        if rules is not None:
            phrases.check_words(rules, (default_language, *spoken))
            phrases = replace(
                phrases,
                accepted={
                    "approve_word": frozenset(_token_set(rules.approve_tokens)),
                    "reject_word": frozenset(_token_set(rules.reject_tokens)),
                },
            )
        return phrases

    def answer_word(self, language: str, key: str) -> str:
        """The word an approval request tells a person to type, one the rules accept.

        The words are checked at load for the languages the deployment declares.
        A session can still end up in another language the catalog holds; when
        that language's word is not one the rules accept, the default language's
        word is shown instead, so a person is never told to type something only
        a model could read.
        """
        word = self.say(language, key)
        tokens = self.accepted.get(key)
        if tokens and normalize(word) not in tokens:
            return self.say(self.default_language, key)
        return word

    def languages(self) -> tuple[str, ...]:
        """Every language the catalog holds, the reference first."""
        others = sorted(language for language in self.catalog if language != REFERENCE_LANGUAGE)
        return (REFERENCE_LANGUAGE, *others)

    def say(self, language: str, key: str, **values: object) -> str:
        """The phrase in this language, or the nearest fallback, with the values inserted."""
        template = self._entry(language, key)
        if isinstance(template, tuple):
            raise PhraseCatalogError(f"phrase {key!r} is a list of words, not a sentence")
        return template.format(**{name: str(value) for name, value in values.items()})

    def words(self, key: str) -> tuple[str, ...]:
        """A list entry, taken from every language the catalog holds, without repeats."""
        if key not in LIST_PHRASES:
            raise PhraseCatalogError(f"phrase {key!r} is a sentence, not a list of words")
        seen: dict[str, None] = {}
        for language in self.languages():
            entry = self.catalog[language].get(key, ())
            for word in entry:
                seen.setdefault(word, None)
        return tuple(seen)

    def check_words(self, rules: RouterRules, languages: Sequence[str]) -> None:
        """Refuse a catalog that tells a person to type what the approval rules do not accept.

        A list of tokens left empty means the rule layer decides nothing of that
        kind and the model router reads the answer, so there is nothing to check.
        """
        accepted = (
            ("approve_word", "router.rules.approve_tokens", _token_set(rules.approve_tokens)),
            ("reject_word", "router.rules.reject_tokens", _token_set(rules.reject_tokens)),
        )
        for language in dict.fromkeys(languages):
            for key, where, tokens in accepted:
                if not tokens:
                    continue
                word = self.say(language, key)
                if normalize(word) not in tokens:
                    raise PhraseCatalogError(
                        f"phrase catalog, language {language!r}: {key} {word!r} is not among "
                        f"{where}, so a person told to type it would not be understood"
                    )

    def _entry(self, language: str, key: str) -> Entry:
        if key not in REQUIRED:
            raise PhraseCatalogError(f"unknown phrase {key!r}")
        for candidate in (language, self.default_language, REFERENCE_LANGUAGE):
            entry = self.catalog.get(candidate, {}).get(key)
            if entry is not None:
                return entry
        raise PhraseCatalogError(f"phrase {key!r} is missing from {REFERENCE_LANGUAGE!r}")


def load_phrases(
    runtime: RuntimeConfig, settings: Settings, layers: Sequence[Path] = ()
) -> Phrases:
    """The phrase catalog this deployment speaks with, checked against its approval rules.

    The configured catalog is laid over the packaged one, and each of `layers`
    over both, in order.
    """
    return Phrases.load(
        phrases_path(runtime.reply, settings),
        *layers,
        default_language=runtime.reply.default_language,
        rules=runtime.router.rules,
        spoken=runtime.discovery.languages,
    )


def _check(catalog: Mapping[str, Mapping[str, Entry]]) -> None:
    """The reference language is complete, and every translation fits its phrase."""
    reference = catalog.get(REFERENCE_LANGUAGE)
    if reference is None:
        raise PhraseCatalogError(
            f"phrase catalog: the reference language {REFERENCE_LANGUAGE!r} is missing"
        )
    missing = [key for key in REQUIRED if key not in reference]
    if missing:
        raise PhraseCatalogError(
            f"phrase catalog, language {REFERENCE_LANGUAGE!r}: missing {', '.join(missing)}"
        )
    for language, entries in catalog.items():
        for key, entry in entries.items():
            if key not in REQUIRED:
                raise PhraseCatalogError(
                    f"phrase catalog, language {language!r}: unknown phrase {key!r}"
                )
            if (key in LIST_PHRASES) != isinstance(entry, tuple):
                shape = "a list of words" if key in LIST_PHRASES else "a sentence"
                raise PhraseCatalogError(
                    f"phrase catalog, language {language!r}: {key} must be {shape}"
                )
            expected = _names(REFERENCE_LANGUAGE, key, reference[key])
            found = _names(language, key, entry)
            if found != expected:
                raise PhraseCatalogError(
                    f"phrase catalog, language {language!r}: {key} names the placeholders "
                    f"{_listed(found)}, and its {REFERENCE_LANGUAGE!r} phrase names "
                    f"{_listed(expected)}"
                )


def _names(language: str, key: str, entry: Entry) -> frozenset[str]:
    """The placeholders a phrase names; a list of words names none."""
    if isinstance(entry, tuple):
        return frozenset()
    try:
        parsed = list(Formatter().parse(entry))
    except ValueError as exc:
        raise PhraseCatalogError(
            f"phrase catalog, language {language!r}: {key} is not a valid template: {exc}"
        ) from exc
    names = set()
    for _, name, spec, conversion in parsed:
        if name is None:
            continue
        if not name.isidentifier() or spec or conversion:
            raise PhraseCatalogError(
                f"phrase catalog, language {language!r}: {key} may only name placeholders "
                f"as {{name}}, and has {{{name}{'!' + conversion if conversion else ''}"
                f"{':' + spec if spec else ''}}}"
            )
        names.add(name)
    return frozenset(names)


def _listed(names: Iterable[str]) -> str:
    ordered = sorted(names)
    return ", ".join(f"{{{name}}}" for name in ordered) if ordered else "none"


def _merge(layers: Sequence[Mapping[str, Mapping[str, Entry]]]) -> dict[str, dict[str, Entry]]:
    """Each layer over the ones before it, phrase by phrase."""
    merged: dict[str, dict[str, Entry]] = {}
    for layer in layers:
        for language, entries in layer.items():
            merged.setdefault(language, {}).update(entries)
    return merged


def read_catalog(path: Path) -> dict[str, dict[str, Entry]]:
    """One catalog file as it is written, before any other layer is laid under it."""
    return _read(path)


def _read(path: Path) -> dict[str, dict[str, Entry]]:
    """One catalog file, shaped as language to phrase to text or list of words."""
    with Path(path).open(encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise PhraseCatalogError(f"{path}: expected a JSON object keyed by language")
    catalog: dict[str, dict[str, Entry]] = {}
    for language, entries in data.items():
        if not isinstance(entries, dict):
            raise PhraseCatalogError(f"{path}: language {language!r} must map phrases to text")
        catalog[language] = {
            key: _entry(path, language, key, value) for key, value in entries.items()
        }
    return catalog


def _entry(path: Path, language: str, key: str, value: object) -> Entry:
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(word, str) and word for word in value):
        return tuple(value)
    raise PhraseCatalogError(
        f"{path}: language {language!r}, {key} must be text or a list of non-empty words"
    )


def _token_set(tokens: Iterable[str]) -> set[str]:
    return {normalize(token) for token in tokens if normalize(token)}
