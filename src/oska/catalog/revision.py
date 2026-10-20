"""A catalog release as an artifact: built once, named by its content, served unchanged.

A directory of skill files is the right catalog for one process and the wrong
one for several. Replicas have no shared disk, modification times differ, and
each replica would pick up a change at its own moment. A revision closes that:
building one reads the skill roots, validates them, copies what held up into a
directory that is never written again, and writes a manifest naming every skill
with the hash of its content. The identifier is derived from that manifest, so
the same inputs always give the same identifier and two replicas that build the
same catalog agree on what to call it.

Nothing here serves a revision; the registry does that. Nothing here decides
which revision is served either, beyond recording the choice a deployment made
in a file the runtime reads back.

A revision may be signed. The build signs the exact bytes of the manifest and
writes the signature beside it; the manifest records the hash of every file the
revision holds, so the signature vouches for the files through it. A store
opened with a `RevisionTrust` checks both before a revision is served, and
refuses one that does not hold up. `signing.py` says who may sign.
"""

from __future__ import annotations

import builtins
import hashlib
import json
import shutil
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from oska.catalog.loader import (
    SkillParseError,
    discover_all,
    fill_composite_capabilities,
    load_skill,
)
from oska.catalog.signing import (
    SIGNATURE_FILENAME,
    RevisionSigner,
    RevisionTrust,
    RevisionTrustError,
    signature_document,
)
from oska.catalog.validator import CatalogError, ValidationIssue, validate_all
from oska.config import Settings
from oska.contracts.skill import Skill

__all__ = [
    "CHOICE_FILENAME",
    "MANIFEST_FILENAME",
    "Manifest",
    "RevisionStore",
    "build_revision",
    "file_hashes",
    "manifest_bytes",
    "revision_id",
]

MANIFEST_FILENAME = "manifest.json"
CHOICE_FILENAME = "CURRENT"
REVISION_LENGTH = 16
_PENDING_PREFIX = ".building-"


class Manifest(BaseModel):
    """What one built revision is: its content, its report and where it came from.

    `skills` maps `<id>@v<version>` to the hash of the file that declared it, so
    two manifests can be compared without reading either catalog. `report` is
    the validation report that let the revision through, kept because a warning
    that was accepted at build time is part of the record of what was served.
    `files` maps every file the revision holds to the hash of its bytes, which
    is what a signature over the manifest vouches for. `source_roots`,
    `created_at` and `files` describe the build, not the catalog, and so take no
    part in the identifier.
    """

    revision: str
    tenant: str
    created_at: datetime
    skills: dict[str, str] = Field(default_factory=dict)
    report: list[str] = Field(default_factory=list)
    source_roots: list[str] = Field(default_factory=list)
    files: dict[str, str] = Field(default_factory=dict)


def manifest_bytes(manifest: Manifest) -> bytes:
    """The canonical bytes of a manifest: what is written, and what is signed."""
    document = json.dumps(manifest.model_dump(mode="json"), indent=2, sort_keys=True)
    return document.encode("utf-8")


def file_hashes(directory: Path) -> dict[str, str]:
    """The hash of every file a revision directory holds, beside its manifest and signature."""
    held: dict[str, str] = {}
    for path in sorted(directory.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(directory).as_posix()
        if relative in (MANIFEST_FILENAME, SIGNATURE_FILENAME):
            continue
        held[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return held


def revision_id(manifest: Manifest) -> str:
    """The identifier a manifest earns from its content.

    Only the tenant and the skills are hashed. Everything else the manifest
    carries is a circumstance of the build, and a catalog that has not changed
    must not be given a new name because it was built again or built elsewhere.
    """
    material = json.dumps(
        {"tenant": manifest.tenant, "skills": manifest.skills},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:REVISION_LENGTH]


def build_revision(
    roots: Sequence[Path],
    store_root: Path,
    *,
    tenant: str,
    settings: Settings | None = None,
    signer: RevisionSigner | None = None,
) -> Manifest:
    """Validate the skill roots and write what they hold as an immutable revision.

    A catalog carrying an error is not released: the issues travel on a
    `CatalogError` and nothing is written. A catalog whose content matches a
    revision the store already holds is not written either; the manifest that is
    already there is returned, so building twice from the same files leaves one
    identifier and one directory.

    With a signer, the manifest is signed and the signature written beside it.
    A revision already held keeps its manifest and files; it gains a signature
    when the one it carries does not verify under this signer, which is how a
    released revision is signed after the fact or under a new key.
    """
    skills, issues = _read(roots, settings)
    blocking = [issue for issue in issues if issue.severity == "error"]
    if blocking:
        raise CatalogError(blocking)
    manifest = Manifest(
        revision="",
        tenant=tenant,
        created_at=datetime.now(UTC),
        skills={f"{skill.id}@v{skill.version}": skill.fingerprint for skill in skills},
        report=[str(issue) for issue in issues],
        source_roots=[str(Path(root)) for root in roots],
    )
    manifest.revision = revision_id(manifest)
    store = RevisionStore(store_root)
    held = store.get(tenant, manifest.revision)
    if held is not None:
        if signer is not None:
            _sign_held(store.path(tenant, held.revision), held, signer)
        return held
    _write(store.path(tenant, manifest.revision), manifest, skills, signer)
    return manifest


class RevisionStore:
    """The revisions a deployment has built, one directory per tenant.

    A revision directory holds the skill files exactly as they were released and
    the manifest beside them. A directory without a readable manifest is not a
    revision and is ignored, so a build interrupted halfway is never served.
    """

    def __init__(self, root: Path | str, *, trust: RevisionTrust | None = None) -> None:
        self._root = Path(root)
        self._trust = trust

    @property
    def root(self) -> Path:
        """Directory the revisions of every tenant are kept under."""
        return self._root

    @property
    def requires_signatures(self) -> bool:
        """Whether this store refuses a revision nobody signed."""
        return self._trust is not None and self._trust.required

    def list(self, tenant: str) -> list[str]:
        """Every revision this tenant holds, oldest build first."""
        built = [
            manifest
            for manifest in (
                self.get(tenant, directory.name) for directory in self._directories(tenant)
            )
            if manifest is not None
        ]
        built.sort(key=lambda manifest: (manifest.created_at, manifest.revision))
        return [manifest.revision for manifest in built]

    def latest(self, tenant: str) -> str:
        """The most recently built revision of this tenant, or nothing when it has none."""
        held = self.list(tenant)
        return held[-1] if held else ""

    def get(self, tenant: str, revision: str) -> Manifest | None:
        """The manifest of one revision, or nothing when the store does not hold it."""
        if not revision:
            return None
        path = self.path(tenant, revision) / MANIFEST_FILENAME
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        try:
            return Manifest.model_validate(document)
        except ValueError:
            return None

    def manifest(self, tenant: str, revision: str) -> Manifest:
        """As `get`, but raises when the store does not hold the revision."""
        held = self.get(tenant, revision)
        if held is None:
            raise KeyError(f"unknown revision: {tenant}/{revision}")
        return held

    def verify(self, tenant: str, revision: str) -> bool:
        """Raise unless this revision may be served; say whether it was signed.

        A store opened without a trust checks nothing and says so by answering
        false. With one, an unsigned revision is refused when signatures are
        required, and a signed one is refused unless its signature holds and
        every file it holds is the file its manifest names.
        """
        if self._trust is None:
            return False
        directory = self.path(tenant, revision)
        try:
            raw = (directory / MANIFEST_FILENAME).read_bytes()
        except OSError as exc:
            raise KeyError(f"unknown revision: {tenant}/{revision}") from exc
        if not self._trust.check(directory, raw):
            return False
        _check_files(directory, Manifest.model_validate_json(raw).files)
        return True

    def path(self, tenant: str, revision: str) -> Path:
        """Where a revision of a tenant lives, whether or not it has been built."""
        return self._root / tenant / revision

    def current(self, tenant: str) -> str:
        """The revision this deployment chose to serve, or nothing when it chose none.

        The choice is a file beside the revisions rather than a line of the
        runtime configuration, so moving a deployment from one release to
        another is an operation and not an edit of a file under review.
        """
        try:
            chosen = (self._root / tenant / CHOICE_FILENAME).read_text(encoding="utf-8")
        except OSError:
            return ""
        chosen = chosen.strip()
        return chosen if self.get(tenant, chosen) is not None else ""

    def choose(self, tenant: str, revision: str) -> None:
        """Record which revision this deployment serves. The revision must exist."""
        self.manifest(tenant, revision)
        target = self._root / tenant / CHOICE_FILENAME
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"{revision}\n", encoding="utf-8")

    def _directories(self, tenant: str) -> builtins.list[Path]:
        base = self._root / tenant
        if not base.is_dir():
            return []
        return sorted(
            path
            for path in base.iterdir()
            if path.is_dir() and not path.name.startswith(_PENDING_PREFIX)
        )


def _read(
    roots: Sequence[Path], settings: Settings | None
) -> tuple[list[Skill], list[ValidationIssue]]:
    """Load every skill under the roots and check it the way the runtime would."""
    skills: list[Skill] = []
    issues: list[ValidationIssue] = []
    for path in discover_all(roots):
        try:
            skills.append(load_skill(path))
        except SkillParseError as exc:
            issues.append(
                ValidationIssue(skill_id="", code="parse_error", message=str(exc), path=path)
            )
    fill_composite_capabilities(skills)
    skills.sort(key=lambda skill: (skill.id, skill.version))
    schema_dir = None if settings is None else settings.schemas_dir
    issues.extend(validate_all(skills, schema_dir=schema_dir, settings=settings))
    return skills, issues


def _write(
    target: Path,
    manifest: Manifest,
    skills: Sequence[Skill],
    signer: RevisionSigner | None,
) -> None:
    """Assemble the revision beside its place and move it there in one step.

    Nothing reads a half-written revision: the files are copied into a directory
    whose name no reader looks at, and only the rename makes it a revision.
    """
    pending = target.parent / f"{_PENDING_PREFIX}{manifest.revision}"
    if pending.exists():
        shutil.rmtree(pending)
    pending.mkdir(parents=True)
    try:
        for skill in skills:
            source = skill.path.parent
            shutil.copytree(
                source,
                pending / skill.type.value / source.parent.name / source.name,
                dirs_exist_ok=True,
            )
        manifest.files = file_hashes(pending)
        written = manifest_bytes(manifest)
        (pending / MANIFEST_FILENAME).write_bytes(written)
        if signer is not None:
            (pending / SIGNATURE_FILENAME).write_bytes(signature_document(signer, written))
        _move(pending, target)
    except OSError:
        shutil.rmtree(pending, ignore_errors=True)
        raise


def _move(pending: Path, target: Path) -> None:
    """Make the assembled directory the revision, unless someone else already did.

    Two processes building the same content build the same identifier, so the
    loser of that race finds the revision already there. It is the same bytes;
    there is nothing to reconcile and nothing to overwrite.
    """
    try:
        pending.rename(target)
    except OSError:
        if not (target / MANIFEST_FILENAME).is_file():
            raise
        shutil.rmtree(pending, ignore_errors=True)


def _sign_held(directory: Path, manifest: Manifest, signer: RevisionSigner) -> None:
    """Sign a revision already released, unless it already carries this signer's signature.

    The manifest is signed as it lies on disk, byte for byte. A manifest from
    before file hashes were recorded cannot vouch for its files and is refused.
    """
    raw = (directory / MANIFEST_FILENAME).read_bytes()
    if not manifest.files:
        raise RevisionTrustError(
            f"revision {manifest.revision} records no file hashes and cannot be signed; "
            "build it again from its sources under a new store"
        )
    try:
        held = json.loads((directory / SIGNATURE_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        held = None
    if (
        isinstance(held, dict)
        and held.get("signer") == signer.name
        and isinstance(held.get("signature"), str)
        and signer.verify(raw, held["signature"])
    ):
        return
    pending = directory.parent / f"{_PENDING_PREFIX}{directory.name}.sig"
    pending.write_bytes(signature_document(signer, raw))
    pending.replace(directory / SIGNATURE_FILENAME)


def _check_files(directory: Path, recorded: dict[str, str]) -> None:
    """Refuse a revision whose files are not the files its signed manifest names."""
    if not recorded:
        raise RevisionTrustError(
            f"the manifest of revision {directory.name} records no file hashes to vouch for"
        )
    found = file_hashes(directory)
    changed = [name for name in sorted(recorded) if found.get(name) != recorded[name]]
    added = [name for name in sorted(found) if name not in recorded]
    if changed or added:
        listed = ", ".join([*changed, *added][:5])
        raise RevisionTrustError(
            f"the files of revision {directory.name} differ from its signed manifest: {listed}"
        )
