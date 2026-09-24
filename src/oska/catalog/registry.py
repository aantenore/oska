"""In-memory catalog, read from a directory that may change or a revision that cannot.

Two deployments are served from here and they differ in one thing. A catalog
read from a directory reloads when the files under it move, which is right for
one process reading its own disk. A catalog read from a built revision is read
once: the revision is immutable, its identifier is its fingerprint, and several
processes serving the same identifier are serving the same bytes.

`ServedCatalog` is the second of those with the revision left open. It holds one
registry per revision and answers through whichever revision the turn under way
is pinned to, so a change published mid-conversation cannot rewrite what a
session was already told.

The two are independent classes over one shape, `Catalog`, which is what the
rest of the runtime holds. Neither is built from the other: a registry reading a
directory and a catalog served out of a store share no state worth inheriting,
and the only thing a caller needs of either is the shape below.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Protocol

from oska.catalog.loader import (
    SkillParseError,
    discover_all,
    fill_composite_capabilities,
    load_skill,
)
from oska.catalog.revision import RevisionStore
from oska.catalog.validator import ValidationIssue
from oska.contracts.common import Hydrated
from oska.contracts.skill import Skill, SkillType

__all__ = ["Catalog", "ServedCatalog", "SkillRegistry", "active_revision"]


class _Pin:
    """The revision the turn under way reads through. One of these per turn."""

    __slots__ = ("revision",)

    def __init__(self, revision: str) -> None:
        self.revision = revision


_PIN: ContextVar[_Pin | None] = ContextVar("oska_catalog_revision", default=None)


def active_revision() -> str:
    """The revision in force in this context, or nothing where none is served."""
    pin = _PIN.get()
    return "" if pin is None else pin.revision


class Catalog(Protocol):
    """What the runtime asks of a catalog, whichever of the two it is holding.

    A catalog answers what the deployment declares, and says which revision it
    answered from. A deployment reading a directory serves one catalog and has
    no revision to name, so `revision` is empty there, `pinned` holds nothing
    and `adopt` moves nothing; a caller therefore never has to ask which kind of
    catalog it was handed.
    """

    @property
    def roots(self) -> list[Path]:
        """Directories this catalog reads."""
        ...

    @property
    def revision(self) -> str:
        """The revision being read here, or nothing when a directory is served."""
        ...

    def newest_revision(self) -> str:
        """The newest revision this deployment could serve right now."""
        ...

    def pinned(self, revision: str) -> AbstractContextManager[None]:
        """Read through one revision for as long as the context is open."""
        ...

    def adopt(self, revision: str) -> None:
        """Change the revision in force for the rest of the open pin."""
        ...

    def fingerprint(self) -> str:
        """What this catalog is, as one string: a hash of its files or a revision."""
        ...

    def skills(self) -> list[Skill]:
        """Every loaded skill file, including superseded versions."""
        ...

    def current(self) -> list[Skill]:
        """The effective version of every skill, sorted by id."""
        ...

    def published(self) -> list[Skill]:
        """The effective version of every published skill, sorted by id."""
        ...

    def by_type(self, skill_type: SkillType | str) -> list[Skill]:
        """The effective version of every skill of one type, sorted by id."""
        ...

    def get(self, skill_id: str) -> Skill | None:
        """The effective version of one skill, or nothing when it is unknown."""
        ...

    def require(self, skill_id: str) -> Skill:
        """As `get`, but raises when the skill is unknown."""
        ...

    def parse_errors(self) -> list[ValidationIssue]:
        """Files that could not be parsed on the last load."""
        ...

    def closure(self, skill_id: str) -> Hydrated:
        """The skill plus every entity, integration and runbook it reaches."""
        ...


class SkillRegistry:
    """Every skill under the configured roots, keyed by id.

    Each public call compares a fingerprint of the discovered files (path, size
    and modification time) with the one held; a difference triggers a reload, so
    an edited or added skill is picked up without a restart. A registry built
    from a revision does not do that: the revision names its own fingerprint and
    the files under it never move, so the catalog is read once and no further.
    """

    def __init__(self, roots: Sequence[Path], *, revision: str = "") -> None:
        self._roots = [Path(root) for root in roots]
        self._revision = revision
        self._fingerprint = ""
        self._skills: list[Skill] = []
        self._current: dict[str, Skill] = {}
        self._errors: list[ValidationIssue] = []
        self._loaded = False

    @classmethod
    def from_revision(cls, store: RevisionStore, tenant: str, revision: str) -> SkillRegistry:
        """A registry over one built revision. An unknown revision raises `KeyError`."""
        manifest = store.manifest(tenant, revision)
        return cls([store.path(tenant, manifest.revision)], revision=manifest.revision)

    @property
    def roots(self) -> list[Path]:
        """Roots this registry reads."""
        return list(self._roots)

    @property
    def revision(self) -> str:
        """The revision this registry serves, or nothing when it reads a directory."""
        return self._revision

    def newest_revision(self) -> str:
        """The newest revision this deployment could serve right now."""
        return self._revision

    @contextmanager
    def pinned(self, revision: str) -> Iterator[None]:
        """Read through one revision for as long as this context is open.

        A registry that serves a single catalog has nothing to pin to and does
        nothing. It is part of the shape rather than an accommodation: a caller
        holding a `Catalog` pins the turn it is running and never asks which of
        the two it was handed.
        """
        yield

    def adopt(self, revision: str) -> None:
        """Change the revision in force for the rest of the open pin."""

    def fingerprint(self) -> str:
        """Fingerprint of the files on disk, or the identifier of the revision serving them."""
        self._refresh()
        return self._fingerprint

    def skills(self) -> list[Skill]:
        """Every loaded skill file, including superseded versions."""
        self._refresh()
        return list(self._skills)

    def current(self) -> list[Skill]:
        """The effective version of every skill, sorted by id."""
        self._refresh()
        return [self._current[key] for key in sorted(self._current)]

    def published(self) -> list[Skill]:
        """The effective version of every published skill, sorted by id."""
        return [skill for skill in self.current() if skill.is_published]

    def by_type(self, skill_type: SkillType | str) -> list[Skill]:
        """The effective version of every skill of one type, sorted by id."""
        wanted = SkillType(skill_type)
        return [skill for skill in self.current() if skill.type is wanted]

    def get(self, skill_id: str) -> Skill | None:
        """The highest published version of a skill, or the highest version if none is published."""
        self._refresh()
        return self._current.get(skill_id)

    def require(self, skill_id: str) -> Skill:
        """As `get`, but raises when the skill is unknown."""
        skill = self.get(skill_id)
        if skill is None:
            raise KeyError(f"unknown skill: {skill_id}")
        return skill

    def parse_errors(self) -> list[ValidationIssue]:
        """Files that could not be parsed on the last load."""
        self._refresh()
        return list(self._errors)

    def closure(self, skill_id: str) -> Hydrated:
        """The skill plus every entity, integration and runbook it reaches."""
        root = self.require(skill_id)
        entities: dict[str, Skill] = {}
        integrations: dict[str, Skill] = {}
        runbooks: dict[str, Skill] = {}
        for member_id in root.frontmatter.runbooks:
            member = self.get(member_id)
            if member is not None:
                runbooks[member_id] = member
        for skill in [root, *runbooks.values()]:
            self._collect(skill.frontmatter.entities, SkillType.entity, entities)
            self._collect(skill.frontmatter.integrations, SkillType.integration, integrations)
        for integration in list(integrations.values()):
            self._collect(integration.frontmatter.entities, SkillType.entity, entities)
        return Hydrated(
            runbook=root,
            entities=[entities[key] for key in sorted(entities)],
            integrations=[integrations[key] for key in sorted(integrations)],
            runbooks=list(runbooks.values()),
        )

    def _collect(
        self,
        references: Iterable[str],
        expected: SkillType,
        into: dict[str, Skill],
    ) -> None:
        for reference in references:
            skill = self.get(reference)
            if skill is not None and skill.type is expected:
                into[reference] = skill

    def _refresh(self) -> None:
        if self._loaded and self._revision:
            return
        paths = discover_all(self._roots)
        fingerprint = self._revision or _fingerprint(paths)
        if self._loaded and fingerprint == self._fingerprint:
            return
        skills: list[Skill] = []
        errors: list[ValidationIssue] = []
        for path in paths:
            try:
                skills.append(load_skill(path))
            except SkillParseError as exc:
                errors.append(
                    ValidationIssue(
                        skill_id="",
                        code="parse_error",
                        message=str(exc),
                        path=path,
                    )
                )
        fill_composite_capabilities(skills)
        self._skills = sorted(skills, key=lambda skill: (skill.id, skill.version))
        self._current = _current_by_id(self._skills)
        self._errors = errors
        self._fingerprint = fingerprint
        self._loaded = True


class ServedCatalog:
    """The catalog a runtime serves out of a revision store.

    One registry is held per revision, built the first time that revision is
    asked for and kept, because a revision cannot change and re-reading it would
    buy nothing. Which revision is answered from belongs to the turn and not to
    the process: one engine serves overlapping turns, so the revision in force
    is held in a context variable that a turn sets for as long as it runs. A
    turn that pins nothing reads the revision the deployment serves.

    What is served is the revision the configuration names; failing that, the
    one the deployment chose and recorded in the store; failing that, the newest
    the store holds. That resolution is re-read rather than cached, so a
    deployment moved to another revision is followed by the next turn that is
    free to adopt it, without a restart.
    """

    def __init__(self, store: RevisionStore, tenant: str, revision: str = "") -> None:
        self._store = store
        self._tenant = tenant
        self._configured = revision
        self._registries: dict[str, SkillRegistry] = {}

    @property
    def store(self) -> RevisionStore:
        """The store this catalog is served out of."""
        return self._store

    @property
    def tenant(self) -> str:
        """The tenant whose catalog this is."""
        return self._tenant

    @property
    def roots(self) -> list[Path]:
        """The directory of the revision being read."""
        return self._active().roots

    @property
    def revision(self) -> str:
        """The revision being read in this context."""
        return self._active().revision

    def newest_revision(self) -> str:
        """The revision this deployment serves now, which a new task may adopt.

        A question and nothing else: it reads the store and leaves this object
        as it found it, so asking what is newest never changes what is served.
        """
        chosen = self._configured or self._store.current(self._tenant)
        return chosen or self._store.latest(self._tenant)

    def for_revision(self, revision: str) -> SkillRegistry:
        """The registry over one revision, built once and kept."""
        held = self._registries.get(revision)
        if held is None:
            held = SkillRegistry.from_revision(self._store, self._tenant, revision)
            self._registries[revision] = held
        return held

    @contextmanager
    def pinned(self, revision: str) -> Iterator[None]:
        """Read through one revision for as long as this context is open."""
        token = _PIN.set(_Pin(revision))
        try:
            yield
        finally:
            _PIN.reset(token)

    def adopt(self, revision: str) -> None:
        """Change the revision in force for the rest of the open pin."""
        pin = _PIN.get()
        if pin is not None:
            pin.revision = revision

    def fingerprint(self) -> str:
        """The identifier of the revision being read, which is what it contains."""
        return self._active().fingerprint()

    def skills(self) -> list[Skill]:
        """Every skill file of the revision being read."""
        return self._active().skills()

    def current(self) -> list[Skill]:
        """The effective version of every skill of the revision being read."""
        return self._active().current()

    def published(self) -> list[Skill]:
        """The effective version of every published skill of the revision being read."""
        return self._active().published()

    def by_type(self, skill_type: SkillType | str) -> list[Skill]:
        """The effective version of every skill of one type, in the revision being read."""
        return self._active().by_type(skill_type)

    def get(self, skill_id: str) -> Skill | None:
        """The effective version of one skill, in the revision being read."""
        return self._active().get(skill_id)

    def require(self, skill_id: str) -> Skill:
        """As `get`, but raises when the revision being read does not hold the skill."""
        return self._active().require(skill_id)

    def parse_errors(self) -> list[ValidationIssue]:
        """Files of the revision being read that could not be parsed."""
        return self._active().parse_errors()

    def closure(self, skill_id: str) -> Hydrated:
        """The skill plus everything it reaches, in the revision being read."""
        return self._active().closure(skill_id)

    def _active(self) -> SkillRegistry:
        """The registry this context reads: the pinned revision, or the served one."""
        pin = _PIN.get()
        revision = pin.revision if pin is not None and pin.revision else ""
        return self.for_revision(revision or self.newest_revision())


def _current_by_id(skills: Sequence[Skill]) -> dict[str, Skill]:
    current: dict[str, Skill] = {}
    for skill in skills:
        held = current.get(skill.id)
        if held is None or _rank(skill) > _rank(held):
            current[skill.id] = skill
    return current


def _rank(skill: Skill) -> tuple[int, int]:
    return (1 if skill.is_published else 0, skill.version)


def _fingerprint(paths: Sequence[Path]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        try:
            stat = path.stat()
        except OSError:
            continue
        digest.update(f"{path}:{stat.st_size}:{stat.st_mtime_ns}\n".encode())
    return digest.hexdigest()
