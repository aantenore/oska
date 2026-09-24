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

Which of the two a deployment serves is `serve_catalog`, and nowhere else. In
`directory` mode it serves the skill roots, or a revision when the store holds
one that answers the configuration, which is how a single process has always
behaved. In `revision` mode it serves a revision and nothing else: with none to
serve, or with one that does not verify, it refuses to start.

The two are independent classes over one shape, `Catalog`, which is what the
rest of the runtime holds. Neither is built from the other: a registry reading a
directory and a catalog served out of a store share no state worth inheriting,
and the only thing a caller needs of either is the shape below.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar
from pathlib import Path
from types import MappingProxyType
from typing import Protocol

from oska.catalog.loader import (
    SkillIntegrityError,
    SkillParseError,
    discover_all,
    fill_composite_capabilities,
    load_skill,
    read_checked,
)
from oska.catalog.revision import RevisionStore
from oska.catalog.signing import RevisionTrust, RevisionTrustError
from oska.catalog.validator import ValidationIssue
from oska.config import CatalogConfig, Settings, state_path
from oska.contracts.common import Hydrated
from oska.contracts.skill import Skill, SkillType

__all__ = [
    "Catalog",
    "CatalogUnavailable",
    "ServedCatalog",
    "SkillRegistry",
    "active_revision",
    "serve_catalog",
]


class CatalogUnavailable(RuntimeError):
    """The configuration asks for a catalog this deployment cannot serve."""


class _Pin:
    """The revision the turn under way reads through. One of these per turn and catalog."""

    __slots__ = ("revision",)

    def __init__(self, revision: str) -> None:
        self.revision = revision


# The pins of this context, one per served catalog, keyed by the catalog's own
# key. A turn that touches two catalogs holds two pins, and neither reads the
# other's. The mapping is replaced, never changed in place, so a context that
# opens a pin leaves the mapping its parent sees as it was.
_PINS: ContextVar[Mapping[object, _Pin]] = ContextVar(
    "oska_catalog_revisions", default=MappingProxyType({})
)
# The pin opened last in this context, for the one reader that has no catalog to
# name: see `active_revision`.
_LAST: ContextVar[_Pin | None] = ContextVar("oska_catalog_last_revision", default=None)


def active_revision(catalog: Catalog | None = None) -> str:
    """The revision in force in this context, or nothing where none is served.

    With a catalog, it is that catalog's pin and no other. Without one, it is the
    pin opened last in this context, which is only an answer where one catalog
    is pinned per turn; a caller holding the catalog names it.
    """
    if catalog is not None:
        return catalog.revision if isinstance(catalog, ServedCatalog) else ""
    pin = _LAST.get()
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

    def read_file(self, path: Path) -> bytes:
        """The bytes of a file beside a skill, checked against the revision that holds it."""
        ...


class SkillRegistry:
    """Every skill under the configured roots, keyed by id.

    Each public call compares a fingerprint of the discovered files (path, size
    and modification time) with the one held; a difference triggers a reload, so
    an edited or added skill is picked up without a restart. A registry built
    from a revision does not do that: the revision names its own fingerprint and
    the files under it never move, so the catalog is read once and no further.
    """

    def __init__(
        self,
        roots: Sequence[Path],
        *,
        revision: str = "",
        files: Mapping[str, str] | None = None,
        strict: bool = False,
    ) -> None:
        self._roots = [Path(root) for root in roots]
        self._revision = revision
        self._files = None if files is None else dict(files)
        self._strict = strict
        self._fingerprint = ""
        self._skills: list[Skill] = []
        self._current: dict[str, Skill] = {}
        self._errors: list[ValidationIssue] = []
        self._loaded = False

    @classmethod
    def from_revision(cls, store: RevisionStore, tenant: str, revision: str) -> SkillRegistry:
        """A registry over one built revision, checked before anything is read from it.

        An unknown revision raises `KeyError`. A revision the store's trust
        refuses raises `RevisionTrustError` and yields no registry at all.

        The files are then read once each, and what is parsed is the bytes
        whose hash was compared with the manifest, so a file changed after the
        store verified the revision is not served. Such a file refuses its
        skill, reported as `revision_file_mismatch`; where the store requires
        signatures it refuses the whole revision with `RevisionTrustError`.
        """
        manifest = store.manifest(tenant, revision)
        store.verify(tenant, manifest.revision)
        registry = cls(
            [store.path(tenant, manifest.revision)],
            revision=manifest.revision,
            files=manifest.files or None,
            strict=store.requires_signatures,
        )
        registry._refresh()
        return registry

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

    def read_file(self, path: Path) -> bytes:
        """The bytes of a file this catalog holds beside its skills, such as a policy.

        Read from a directory, the file is read as it is. Read from a revision,
        it is read once and returned only when its bytes hash to what the
        manifest records; a file the manifest does not name, or one changed
        since the revision was built, raises `SkillIntegrityError`.
        """
        target = Path(path)
        return read_checked(target, self._expected_hash(target))

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
                skills.append(load_skill(path, expected_hash=self._expected_hash(path)))
            except SkillIntegrityError as exc:
                if self._strict:
                    raise RevisionTrustError(str(exc)) from exc
                errors.append(
                    ValidationIssue(
                        skill_id="",
                        code="revision_file_mismatch",
                        message=str(exc),
                        path=path,
                    )
                )
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

    def _expected_hash(self, path: Path) -> str | None:
        """The hash the revision's manifest records for this file, when it records any.

        A file the manifest does not name gets an empty hash, which nothing has.
        """
        if self._files is None or not self._roots:
            return None
        try:
            relative = path.resolve().relative_to(self._roots[0].resolve()).as_posix()
        except ValueError:
            return ""
        return self._files.get(relative, "")


class ServedCatalog:
    """The catalog a runtime serves out of a revision store.

    One registry is held per revision, built the first time that revision is
    asked for and kept, because a revision cannot change and re-reading it would
    buy nothing. Which revision is answered from belongs to the turn and not to
    the process: one engine serves overlapping turns, so the revision in force
    is held in a context variable that a turn sets for as long as it runs,
    keyed by this catalog, so a turn that touches two catalogs holds a pin for
    each and neither reads the other's. A turn that pins nothing reads the
    revision the deployment serves.

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
        self._key = object()

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
        """Read through one revision for as long as this context is open.

        The pin is this catalog's alone: another catalog pinned in the same
        context keeps its own.
        """
        pin = _Pin(revision)
        pins = _PINS.set({**_PINS.get(), self._key: pin})
        last = _LAST.set(pin)
        try:
            yield
        finally:
            _LAST.reset(last)
            _PINS.reset(pins)

    def adopt(self, revision: str) -> None:
        """Change the revision in force for the rest of this catalog's open pin."""
        pin = _PINS.get().get(self._key)
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

    def read_file(self, path: Path) -> bytes:
        """The bytes of a file beside a skill, checked against the revision that holds it.

        The revision is the one whose directory holds the file, so a policy is
        checked against the manifest of the skill that names it; a file under
        no revision held here is checked against the revision being read.
        """
        target = Path(path).resolve()
        for registry in list(self._registries.values()):
            root = registry.roots[0].resolve()
            if target.is_relative_to(root):
                return registry.read_file(target)
        return self._active().read_file(target)

    def _active(self) -> SkillRegistry:
        """The registry this context reads: the pinned revision, or the served one."""
        pin = _PINS.get().get(self._key)
        revision = pin.revision if pin is not None and pin.revision else ""
        return self.for_revision(revision or self.newest_revision())


def serve_catalog(
    config: CatalogConfig,
    settings: Settings,
    *,
    tenant: str = "",
    env: Mapping[str, str] | None = None,
) -> Catalog:
    """The catalog this deployment serves, chosen by `config.mode`.

    `directory` serves the skill roots of `settings`, unless the store holds a
    revision that answers the configuration: the one it names, or failing a
    name the one the deployment chose or the newest. A named revision the store
    does not hold falls back to the roots. `revision` serves a revision and
    nothing else, and raises `CatalogUnavailable` when there is none to serve.

    Whichever revision is chosen is checked against `config.signing` here, so a
    revision that does not verify stops the process at start with
    `RevisionTrustError` rather than at its first turn. `env` is where signing
    keys are read from; the process environment when it is not given.
    """
    chosen = tenant.strip() or config.tenant
    store = RevisionStore(
        state_path(settings.state_dir, config.revisions_root),
        trust=RevisionTrust(config.signing, env),
    )
    named = config.revision
    if named and store.get(chosen, named) is not None:
        return _checked(ServedCatalog(store, chosen, named))
    held = not named and bool(store.current(chosen) or store.latest(chosen))
    if config.mode == "revision":
        if not held:
            wanted = f"revision {named}" if named else "a revision"
            raise CatalogUnavailable(
                f"catalog.mode is revision, and {store.root / chosen} does not hold "
                f"{wanted} to serve; build one with `oska revision build`"
            )
        return _checked(ServedCatalog(store, chosen))
    if held:
        return _checked(ServedCatalog(store, chosen))
    return SkillRegistry(settings.skills_roots)


def _checked(served: ServedCatalog) -> ServedCatalog:
    """The served catalog, once the revision it serves now has been verified."""
    served.for_revision(served.newest_revision())
    return served


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
