"""The governed catalog of guardrails, and the checks that keep some of them.

A guardrail is a rule the installation promises to keep. The promise is written
once, in the catalog document, and a skill references it by identifier; nothing
restates it. Each entry says where it acts and what keeps it, and there are
three ways a promise is kept. A `check` is code this module runs at a named
point of the turn and that can block or redact there. An `invariant` is a
promise a named module keeps on its own, at a point this module runs nothing:
it declares where the promise holds and who holds it, and carries no validator
and no parameters, because there is nothing here to configure. An `instruction`
is text handed to a model and nothing more.

Keeping those three apart is the reason the catalog exists. A sentence in a
prompt and a branch in the code are not the same kind of promise, and neither
is a branch somewhere else: an entry that names a validator and parameters at a
point nothing reads them reads as control while providing none, and editing
those parameters would change nothing. So a `check` whose point this module
does not run is refused when the catalog is read, and an entry that is kept
elsewhere has to say so and name the module that keeps it.

Nothing in this module carries a pattern, a threshold or a word list. Every
check is built from the parameters its catalog entry declares, so what a
guardrail refuses can be read, reviewed and changed in the document rather than
in the code.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field, model_validator

from oska.contracts.skill import Skill

__all__ = [
    "CHAIN_POINTS",
    "Check",
    "CheckResult",
    "Enforcement",
    "Guardrail",
    "GuardrailCatalog",
    "GuardrailChain",
    "GuardrailError",
    "GuardrailOutcome",
    "Mechanism",
    "Point",
    "forbidden_pattern",
    "instruction_block",
    "member_of_declared",
    "registered_checks",
    "value_in_set",
]

Point = Literal["input", "planning", "execution", "output"]
Mechanism = Literal["check", "instruction", "invariant"]
Action = Literal["passed", "blocked", "redacted"]

CATALOG_KEY = "guardrails"
PREAMBLE = "Promises this installation has declared, and holds you to:"

CHAIN_POINTS: tuple[Point, ...] = ("input", "output")


class GuardrailError(ValueError):
    """Raised when the catalog does not hold up, or a skill names an entry it lacks."""


class Enforcement(BaseModel):
    """Where a guardrail acts and what keeps it."""

    point: Point
    mechanism: Mechanism
    validator: str | None = None
    module: str | None = None
    parameters: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def _mechanism_matches_what_keeps_it(self) -> Enforcement:
        if self.mechanism == "check":
            if not self.validator:
                raise ValueError("a check must name the validator that runs it")
            if self.point not in CHAIN_POINTS:
                raise ValueError(
                    f"a check runs at {' or '.join(CHAIN_POINTS)}, and nothing runs "
                    f"checks at {self.point}; a promise kept there is an invariant, "
                    "which names the module that keeps it"
                )
            return self
        if self.validator:
            raise ValueError(
                f"an {self.mechanism} is not run from the catalog, so it names no validator"
            )
        if self.parameters:
            raise ValueError(
                f"an {self.mechanism} has no validator to read parameters, so it declares none"
            )
        if self.mechanism == "invariant" and not self.module:
            raise ValueError("an invariant must name the module that keeps it")
        return self


class Guardrail(BaseModel):
    """One entry of the catalog: the promise, and how it is kept."""

    id: str
    title: str
    description: str = Field(min_length=1)
    enforcement: Enforcement

    @property
    def is_check(self) -> bool:
        """Whether the chain runs code for this guardrail and can refuse."""
        return self.enforcement.mechanism == "check"

    @property
    def is_invariant(self) -> bool:
        """Whether a named module keeps this promise where the chain runs nothing."""
        return self.enforcement.mechanism == "invariant"


@dataclass(frozen=True)
class CheckResult:
    """What one check decided about one subject."""

    ok: bool
    reason: str = ""
    redacted_text: str | None = None


@runtime_checkable
class Check(Protocol):
    """A rule that can look at the text of a turn, or at a payload, and refuse it."""

    def __call__(
        self, subject: str | Mapping[str, object], context: Mapping[str, object]
    ) -> CheckResult:
        """Decide whether this subject may pass."""
        ...


CheckFactory = Callable[[Mapping[str, object]], Check]

PASSED = CheckResult(ok=True)


def forbidden_pattern(parameters: Mapping[str, object]) -> Check:
    """Refuse text matching any declared pattern, and hand back a redacted copy.

    `patterns` are regular expressions, `replacement` is what a match is
    replaced with in the copy, `reason` is what the refusal says. All three come
    from the catalog entry.
    """
    expressions = [re.compile(pattern) for pattern in _strings(parameters, "patterns")]
    replacement = str(parameters.get("replacement", ""))
    reason = str(parameters.get("reason", ""))

    def _check(
        subject: str | Mapping[str, object], context: Mapping[str, object]
    ) -> CheckResult:
        text = subject if isinstance(subject, str) else json.dumps(dict(subject), sort_keys=True)
        redacted = text
        for expression in expressions:
            redacted = expression.sub(replacement, redacted)
        if redacted == text:
            return PASSED
        return CheckResult(ok=False, reason=reason, redacted_text=redacted)

    return _check


def value_in_set(parameters: Mapping[str, object]) -> Check:
    """Refuse a payload whose declared field does not hold one of the allowed values.

    `field` is a dotted path into the payload, `allowed` is the closed set the
    value has to be in, `reason` is what the refusal says.
    """
    path = str(parameters.get("field", ""))
    allowed = [str(value) for value in _values(parameters, "allowed")]
    reason = str(parameters.get("reason", ""))

    def _check(
        subject: str | Mapping[str, object], context: Mapping[str, object]
    ) -> CheckResult:
        value = _at(subject, path)
        if value is not None and str(value) in allowed:
            return PASSED
        return CheckResult(ok=False, reason=reason)

    return _check


def member_of_declared(parameters: Mapping[str, object]) -> Check:
    """Refuse a payload whose declared field is not among what the context declares.

    `field` is a dotted path into the payload, `declared` is the key of the
    context holding the values that are allowed, `reason` is what the refusal
    says. The allowed values come from the turn, not from this file.
    """
    path = str(parameters.get("field", ""))
    declared_key = str(parameters.get("declared", ""))
    reason = str(parameters.get("reason", ""))

    def _check(
        subject: str | Mapping[str, object], context: Mapping[str, object]
    ) -> CheckResult:
        declared = [str(value) for value in _values(context, declared_key)]
        value = _at(subject, path)
        if value is not None and str(value) in declared:
            return PASSED
        return CheckResult(ok=False, reason=reason)

    return _check


_REGISTRY: dict[str, CheckFactory] = {
    "forbidden_pattern": forbidden_pattern,
    "value_in_set": value_in_set,
    "member_of_declared": member_of_declared,
}


def registered_checks() -> list[str]:
    """Names a catalog entry may use as its validator."""
    return sorted(_REGISTRY)


class GuardrailCatalog:
    """Every guardrail this installation declares, read from one document."""

    def __init__(self, guardrails: Iterable[Guardrail]) -> None:
        self._guardrails: dict[str, Guardrail] = {}
        for guardrail in guardrails:
            if guardrail.id in self._guardrails:
                raise GuardrailError(f"{guardrail.id} is declared more than once")
            if guardrail.is_check and guardrail.enforcement.validator not in _REGISTRY:
                raise GuardrailError(
                    f"{guardrail.id} names the validator "
                    f"{guardrail.enforcement.validator!r}, which is not registered; "
                    f"registered: {', '.join(registered_checks())}"
                )
            self._guardrails[guardrail.id] = guardrail
        self._checks: dict[str, Check] = {
            guardrail.id: _REGISTRY[str(guardrail.enforcement.validator)](
                guardrail.enforcement.parameters
            )
            for guardrail in self._guardrails.values()
            if guardrail.is_check
        }

    @classmethod
    def load(cls, path: Path) -> GuardrailCatalog:
        """Read the catalog document."""
        target = Path(path)
        try:
            document = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise GuardrailError(
                f"{target}: the guardrail catalog cannot be read: {error}"
            ) from error
        if not isinstance(document, dict):
            raise GuardrailError(f"{target}: expected a JSON object")
        entries = document.get(CATALOG_KEY)
        if not isinstance(entries, dict):
            raise GuardrailError(f"{target}: expected an object under {CATALOG_KEY!r}")
        guardrails = []
        for guardrail_id, entry in entries.items():
            if not isinstance(entry, dict):
                raise GuardrailError(f"{target}: {guardrail_id} is not an object")
            try:
                guardrails.append(Guardrail.model_validate({**entry, "id": guardrail_id}))
            except ValueError as error:
                raise GuardrailError(
                    f"{target}: {guardrail_id} does not hold up: {error}"
                ) from error
        return cls(guardrails)

    @classmethod
    def empty(cls) -> GuardrailCatalog:
        """A catalog that declares nothing, used when guardrails are switched off."""
        return cls([])

    def __len__(self) -> int:
        return len(self._guardrails)

    def ids(self) -> list[str]:
        """Every identifier the catalog defines, in order."""
        return list(self._guardrails)

    def all(self) -> list[Guardrail]:
        """Every entry the catalog defines, in order."""
        return list(self._guardrails.values())

    def get(self, guardrail_id: str) -> Guardrail | None:
        """The entry with this identifier, or nothing."""
        return self._guardrails.get(guardrail_id)

    def require(self, guardrail_id: str) -> Guardrail:
        """The entry with this identifier; an identifier the catalog lacks is an error."""
        guardrail = self._guardrails.get(guardrail_id)
        if guardrail is None:
            raise GuardrailError(
                f"{guardrail_id} is not declared by the guardrail catalog; "
                f"it declares {', '.join(self.ids()) or 'nothing'}"
            )
        return guardrail

    def for_skill(self, skill: Skill) -> list[Guardrail]:
        """The guardrails this skill declares; one it names and the catalog lacks is an error."""
        return [self.require(name) for name in skill.frontmatter.guardrails]

    def instructions(
        self, skills: Sequence[Skill], point: Point | None = None
    ) -> list[str]:
        """The prose of every instruction guardrail these skills declare, without repeats.

        This is the only text a model is given about the promises. A guardrail
        kept by a check is left out, because handing the model a rule that code
        already enforces invites it to argue with the code.
        """
        prose: list[str] = []
        for skill in skills:
            for guardrail in self.for_skill(skill):
                enforcement = guardrail.enforcement
                if enforcement.mechanism != "instruction":
                    continue
                if point is not None and enforcement.point != point:
                    continue
                if guardrail.description not in prose:
                    prose.append(guardrail.description)
        return prose

    def checks(self, phase: Point) -> list[tuple[Guardrail, Check]]:
        """Every guardrail the chain runs at this point, with the check that runs it.

        Only the points the chain runs can answer with anything: an entry is
        refused at load when it declares a check anywhere else, so a point that
        is not one of them holds no check to return.
        """
        return [
            (guardrail, self._checks[guardrail.id])
            for guardrail in self._guardrails.values()
            if guardrail.is_check and guardrail.enforcement.point == phase
        ]


@dataclass(frozen=True)
class GuardrailOutcome:
    """What the chain did to one text: which guardrail acted, and what it did."""

    text: str
    ok: bool = True
    action: Action = "passed"
    guardrail_id: str | None = None
    reason: str = ""
    description: str = ""

    def as_event(self, point: Point) -> dict:
        """The audit data for this outcome."""
        return {
            "point": point,
            "guardrail_id": self.guardrail_id,
            "action": self.action,
            "reason": self.reason,
        }


@dataclass
class GuardrailChain:
    """The checks of one point, run in catalog order over the text of a turn.

    The first guardrail that refuses ends the chain: what it says is what the
    turn answers with, and nothing behind it is asked. A guardrail that only
    redacts hands its copy to the next one, so several may act on one text.
    """

    catalog: GuardrailCatalog
    enabled: bool = True
    context: dict = field(default_factory=dict)

    def instructions(
        self, skills: Sequence[Skill], point: Point | None = None
    ) -> list[str]:
        """The prose of the instruction guardrails these skills declare.

        A deployment that has switched its guardrails off states none of them:
        the catalog it was handed declares nothing, and a skill that names an
        entry is not held to a promise this installation is not keeping.
        """
        if not self.enabled:
            return []
        return self.catalog.instructions(skills, point)

    def apply_input(
        self, text: str, context: Mapping[str, object] | None = None
    ) -> GuardrailOutcome:
        """Run the input checks over what the user typed, before anything is decided."""
        return self._apply("input", text, context)

    def apply_output(
        self, text: str, context: Mapping[str, object] | None = None
    ) -> GuardrailOutcome:
        """Run the output checks over the reply, before it is returned."""
        return self._apply("output", text, context)

    def _apply(
        self, point: Point, text: str, context: Mapping[str, object] | None
    ) -> GuardrailOutcome:
        if not self.enabled:
            return GuardrailOutcome(text=text)
        merged: dict = {**self.context, **dict(context or {})}
        current = text
        redactor: Guardrail | None = None
        for guardrail, check in self.catalog.checks(point):
            result = check(current, merged)
            if result.redacted_text is not None and result.redacted_text != current:
                current = result.redacted_text
                redactor = guardrail
            if result.ok:
                continue
            return GuardrailOutcome(
                text=current,
                ok=False,
                action="blocked",
                guardrail_id=guardrail.id,
                reason=result.reason,
                description=guardrail.description,
            )
        if redactor is not None:
            return GuardrailOutcome(
                text=current,
                action="redacted",
                guardrail_id=redactor.id,
                description=redactor.description,
            )
        return GuardrailOutcome(text=current)


def instruction_block(prose: Sequence[str]) -> str:
    """Several guardrail descriptions as one block of prompt text, or nothing."""
    if not prose:
        return ""
    return PREAMBLE + "\n" + "\n".join(f"- {line}" for line in prose)


def _at(subject: str | Mapping[str, object], path: str) -> object | None:
    """The value at a dotted path of a payload; a text subject holds no fields."""
    if isinstance(subject, str) or not path:
        return None
    value: object = subject
    for part in path.split("."):
        if not isinstance(value, Mapping):
            return None
        value = value.get(part)
        if value is None:
            return None
    return value


def _values(source: Mapping[str, object], key: str) -> list[object]:
    value = source.get(key) if key else None
    if isinstance(value, (list, tuple, set)):
        return list(value)
    if value is None:
        return []
    return [value]


def _strings(source: Mapping[str, object], key: str) -> list[str]:
    return [str(value) for value in _values(source, key)]
