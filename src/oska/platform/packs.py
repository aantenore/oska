"""A domain pack on disk: its manifest, its fixed layout, and what it depends on.

A pack is a directory holding `pack.yaml` and a fixed layout under it:
`skills/` (required), `configs/connectors.json` (required), `fixtures/`,
`scenarios/`, `evaluation/`, and an optional `guardrails.json` whose entries are
added to the platform guardrail catalog. Nothing here runs a pack; this module
reads one, checks that it declares what it says it provides, and orders a set
of packs so that every pack comes after the packs it depends on.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import ValidationError

import oska
from oska.catalog.registry import Catalog
from oska.catalog.resolver import PROCESS_TYPES
from oska.contracts.platform import PackManifest
from oska.contracts.skill import SkillType
from oska.platform.errors import PackError
from oska.platform.versions import satisfies

__all__ = [
    "PACK_FILENAME",
    "RUNTIME_REQUIREMENT",
    "Pack",
    "check_provides",
    "check_requires",
    "load_pack",
    "resolve_dependencies",
]

PACK_FILENAME = "pack.yaml"
RUNTIME_REQUIREMENT = "oska"
SKILLS_DIR = "skills"
CONNECTORS_FILE = Path("configs") / "connectors.json"
FIXTURES_DIR = "fixtures"
SCENARIOS_DIR = "scenarios"
EVALUATION_DIR = "evaluation"
GUARDRAILS_FILE = "guardrails.json"


@dataclass(frozen=True)
class Pack:
    """One pack: its manifest and where each part of its layout is."""

    manifest: PackManifest
    root: Path

    @property
    def name(self) -> str:
        """The name the pack is known by."""
        return self.manifest.name

    @property
    def version(self) -> str:
        """The version of the pack."""
        return self.manifest.version

    @property
    def skills(self) -> Path:
        """The catalog root of the pack."""
        return self.root / SKILLS_DIR

    @property
    def connectors(self) -> Path:
        """The file binding the pack's integrations to connectors."""
        return self.root / CONNECTORS_FILE

    @property
    def fixtures(self) -> Path:
        """Where the mock bindings read their data."""
        return self.root / FIXTURES_DIR

    @property
    def scenarios(self) -> Path:
        """The conformance scenarios of the pack."""
        return self.root / SCENARIOS_DIR

    @property
    def evaluation(self) -> Path:
        """The evaluation sets of the pack."""
        return self.root / EVALUATION_DIR

    @property
    def guardrails(self) -> Path | None:
        """The pack's own guardrail entries, when it declares any."""
        path = self.root / GUARDRAILS_FILE
        return path if path.is_file() else None


def load_pack(path: Path | str) -> Pack:
    """Read a pack from its directory, or from its `pack.yaml`.

    A pack without its catalog root or without its connector bindings is not a
    pack; the other parts of the layout are optional and are read by whoever
    needs them.
    """
    target = Path(path)
    manifest_path = target if target.is_file() else target / PACK_FILENAME
    root = manifest_path.parent.resolve()
    if not manifest_path.is_file():
        raise PackError(f"{target}: no {PACK_FILENAME} here")
    try:
        document = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise PackError(f"{manifest_path}: cannot be read: {exc}") from exc
    if not isinstance(document, dict):
        raise PackError(f"{manifest_path}: expected a mapping")
    try:
        manifest = PackManifest.model_validate(document)
    except ValidationError as exc:
        raise PackError(f"{manifest_path}: {_first_error(exc)}") from exc
    pack = Pack(manifest=manifest, root=root)
    if not pack.skills.is_dir():
        raise PackError(f"pack {pack.name}: no {SKILLS_DIR}/ directory under {root}")
    if not pack.connectors.is_file():
        raise PackError(f"pack {pack.name}: no {CONNECTORS_FILE} under {root}")
    return pack


def check_provides(pack: Pack, registry: Catalog) -> None:
    """Refuse a pack whose `provides` and whose skills disagree.

    `entities` lists every entity the pack declares and `procedures` every
    runbook and composite. A name the pack claims and does not declare is
    refused, and so is a skill it declares and does not list.
    """
    declared_entities = {
        skill.id for skill in registry.current() if skill.type is SkillType.entity
    }
    declared_procedures = {
        skill.id for skill in registry.current() if skill.type in PROCESS_TYPES
    }
    problems: list[str] = []
    for kind, claimed, declared in (
        ("entities", set(pack.manifest.provides.entities), declared_entities),
        ("procedures", set(pack.manifest.provides.procedures), declared_procedures),
    ):
        for name in sorted(claimed - declared):
            problems.append(f"provides {kind} {name}, which it does not declare")
        for name in sorted(declared - claimed):
            problems.append(f"declares {name}, which provides.{kind} omits")
    if problems:
        raise PackError(f"pack {pack.name}: {'; '.join(problems)}")


def check_requires(pack: Pack, runtime_version: str | None = None) -> None:
    """Refuse a pack that does not accept the version of the runtime it is served by."""
    wanted = pack.manifest.requires.get(RUNTIME_REQUIREMENT)
    if wanted is None:
        return
    held = runtime_version or oska.__version__
    try:
        accepted = satisfies(held, wanted)
    except ValueError as exc:
        raise PackError(f"pack {pack.name}: requires {RUNTIME_REQUIREMENT} {exc}") from exc
    if not accepted:
        raise PackError(
            f"pack {pack.name} requires {RUNTIME_REQUIREMENT} {wanted}, and this runtime is {held}"
        )


def resolve_dependencies(packs: Iterable[Pack]) -> list[Pack]:
    """The packs ordered so that each comes after the packs it depends on.

    A dependency that is not among the packs, a version outside the range the
    dependent accepts, two packs with one name, and a cycle are each refused
    with the names involved.
    """
    by_name: dict[str, Pack] = {}
    for pack in packs:
        if pack.name in by_name:
            raise PackError(f"two packs are named {pack.name}")
        by_name[pack.name] = pack
    for pack in by_name.values():
        for dependency in pack.manifest.depends_on:
            found = by_name.get(dependency.pack)
            if found is None:
                raise PackError(
                    f"pack {pack.name} depends on {dependency.pack} {dependency.version}, "
                    "which is not deployed beside it"
                )
            if not _accepts(found.version, dependency.version, pack.name):
                raise PackError(
                    f"pack {pack.name} depends on {dependency.pack} {dependency.version}, "
                    f"and {dependency.pack} is {found.version}"
                )
    ordered: list[Pack] = []
    visiting: list[str] = []
    done: set[str] = set()

    def visit(name: str) -> None:
        if name in done:
            return
        if name in visiting:
            cycle = [*visiting[visiting.index(name):], name]
            raise PackError(f"packs depend on each other in a cycle: {' -> '.join(cycle)}")
        visiting.append(name)
        for dependency in by_name[name].manifest.depends_on:
            visit(dependency.pack)
        visiting.pop()
        done.add(name)
        ordered.append(by_name[name])

    for name in by_name:
        visit(name)
    return ordered


def _accepts(version: str, spec: str, dependent: str) -> bool:
    try:
        return satisfies(version, spec)
    except ValueError as exc:
        raise PackError(f"pack {dependent}: {exc}") from exc


def _first_error(exc: ValidationError) -> str:
    for error in exc.errors():
        location = ".".join(str(part) for part in error.get("loc", ()))
        return f"{location}: {error.get('msg', 'invalid value')}".strip(": ")
    return str(exc)

