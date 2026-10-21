"""A proposed catalog change, written for a person to read and apply.

Publication used to be a file written under the running server. It is now a
directory written under the change request root: the skill package exactly as
it would be published, the record of who asked for it and what the quality gate
said, and a summary a reviewer can read without opening anything else. The
process that answers people composes the request; it never installs it.

Installing is `apply`, and `apply` cannot guess where to install: the catalog
root is an argument, given by whoever calls it. Nothing in the engine, the HTTP
application or the improvement job calls it, and nothing in this module knows
which root a deployment serves.

The layout under the change request root is one directory per request::

    <root>/<request id>/request.json
    <root>/<request id>/summary.md
    <root>/<request id>/<type>/<name>/v<N>/skill.md
    <root>/<request id>/<type>/<name>/v<N>/policy.json

The package is laid out the way a catalog root is laid out, so applying a
request is a copy of one subtree and not a translation.

`ChangeRequestStore` is the directory-backed `ChangeRequestRecordStore`
(`oska.contracts.stores`). With `storage.backend: postgres` the same requests,
packages included, are kept in the database instead, so every replica reads one
review queue; `oska.services.open_changes` opens whichever the configuration
names.

A request may also be published as a branch of a git repository, for a person
to review there. `publishers.py` does that and records the branch and the
commit on the request; the request stays open until a person decides on it.
"""

from __future__ import annotations

import builtins
import json
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path

from oska.config import Settings, load_runtime_config, state_path
from oska.contracts.change import (
    POLICY_FILENAME,
    SKILL_FILENAME,
    ChangeRequest,
    ChangeRequestError,
    ChangeRequestState,
    package_file,
    require_actor,
    require_open,
)
from oska.contracts.placement import claim_target
from oska.contracts.proposal import VERIFIED_SOURCES, ActorSource, QualityReport

__all__ = [
    "POLICY_FILENAME",
    "REQUEST_FILENAME",
    "SKILL_FILENAME",
    "SUMMARY_FILENAME",
    "ChangeRequest",
    "ChangeRequestError",
    "ChangeRequestState",
    "ChangeRequestStore",
    "new_request",
    "package_file",
    "package_of",
    "require_actor",
    "require_open",
    "store_for",
]

REQUEST_FILENAME = "request.json"
SUMMARY_FILENAME = "summary.md"


def package_of(skill_id: str, version: int) -> str:
    """Where the package sits inside the request, laid out as a catalog root is.

    A skill identifier is its type and its name, and that is exactly the first
    two levels of a catalog root, so the path is read off the identifier rather
    than decided here.
    """
    kind, _, name = skill_id.partition(".")
    if not kind or not name:
        raise ChangeRequestError(f"a skill id is a type and a name: {skill_id!r}")
    return f"{kind}/{name}/v{version}"


def new_request(
    proposal_id: str,
    skill_id: str,
    version: int,
    actor: str,
    quality: QualityReport,
    *,
    actor_source: ActorSource | str = ActorSource.stated,
) -> ChangeRequest:
    """An open request for one package, stamped with the moment it was composed."""
    source = ActorSource(actor_source)
    return ChangeRequest(
        request_id=str(uuid.uuid4()),
        proposal_id=proposal_id,
        skill_id=skill_id,
        version=version,
        actor=actor,
        actor_source=source,
        actor_verified=source in VERIFIED_SOURCES,
        created_at=datetime.now(UTC),
        quality=quality,
        package=package_of(skill_id, version),
    )


class ChangeRequestStore:
    """Change requests kept as one directory each, named after the request id."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        """Where the requests are kept."""
        return self._root

    @property
    def location(self) -> str:
        """The change request root, as a person finds it."""
        return str(self._root)

    def directory(self, request_id: str) -> Path:
        """The directory of one request, whether or not it is there yet."""
        name = Path(request_id).name
        if not name or name != request_id:
            raise ChangeRequestError(f"unusable request id: {request_id!r}")
        return self._root / name

    def package(self, request_id: str) -> Path:
        """The package directory of one request, laid out as a catalog root is."""
        return self.directory(request_id) / self.get(request_id).package

    def save(
        self,
        request: ChangeRequest,
        *,
        skill_markdown: str | None = None,
        policy: dict | None = None,
    ) -> Path:
        """Write a request, and its package when the file is given.

        A decision rewrites the record and leaves the package alone, which is
        why the markdown is optional: what a reviewer approved has to stay the
        bytes they read.
        """
        directory = self.directory(request.request_id)
        directory.mkdir(parents=True, exist_ok=True)
        if skill_markdown is not None:
            package = directory / request.package
            package.mkdir(parents=True, exist_ok=True)
            (package / SKILL_FILENAME).write_text(skill_markdown, encoding="utf-8")
            if policy is not None:
                (package / POLICY_FILENAME).write_text(
                    json.dumps(policy, indent=2) + "\n", encoding="utf-8"
                )
        self._write(request)
        return directory

    def get(self, request_id: str) -> ChangeRequest:
        """Read one request. Raises `KeyError` when it is not there."""
        path = self.directory(request_id) / REQUEST_FILENAME
        if not path.is_file():
            raise KeyError(f"unknown change request: {request_id}")
        return ChangeRequest.model_validate_json(path.read_text(encoding="utf-8"))

    def list(self, state: ChangeRequestState | str | None = None) -> list[ChangeRequest]:
        """Every request held, oldest first, filtered by state when one is named."""
        wanted = ChangeRequestState(state) if state is not None else None
        held: list[ChangeRequest] = []
        if not self._root.is_dir():
            return held
        for path in sorted(self._root.glob(f"*/{REQUEST_FILENAME}")):
            request = ChangeRequest.model_validate_json(path.read_text(encoding="utf-8"))
            if wanted is None or request.state is wanted:
                held.append(request)
        held.sort(key=lambda request: (request.created_at, request.request_id))
        return held

    def files(self, request_id: str) -> dict[str, str]:
        """The files of one request's package, by their path inside it."""
        source = self.directory(request_id) / self.get(request_id).package
        if not source.is_dir():
            return {}
        return {
            path.relative_to(source).as_posix(): path.read_text(encoding="utf-8")
            for path in sorted(source.rglob("*"))
            if path.is_file()
        }

    def add_file(self, request_id: str, name: str, text: str) -> None:
        """Write one more file into a request's package, replacing one of the same name."""
        path = self.package(request_id) / package_file(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def write_package(self, request_id: str, target: Path) -> builtins.list[Path]:
        """Copy the package of one request into a directory, and name the files written."""
        source = self.directory(request_id) / self.get(request_id).package
        if not source.is_dir():
            raise ChangeRequestError(f"change request {request_id} holds no package")
        shutil.copytree(source, target)
        return sorted(path for path in Path(target).rglob("*") if path.is_file())

    def delete(self, request_id: str) -> None:
        """Remove one request and its package. Raises `KeyError` when it is not there."""
        directory = self.directory(request_id)
        if not directory.is_dir():
            raise KeyError(f"unknown change request: {request_id}")
        shutil.rmtree(directory)

    def drop_package(self, request_id: str) -> bool:
        """Remove everything but the record and its summary, and say whether anything went."""
        directory = self.directory(request_id)
        removed = False
        for entry in sorted(directory.iterdir()):
            if entry.name in (REQUEST_FILENAME, SUMMARY_FILENAME):
                continue
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()
            removed = True
        return removed

    def purge(self) -> int:
        """Remove nothing: the namespace of a directory store is its directory, not its own."""
        return 0

    def apply(self, request_id: str, catalog_root: Path, *, by: str) -> builtins.list[Path]:
        """Copy the package of one open request into a catalog root a person names.

        This is the only thing in the evolution layer that writes into a
        catalog, and it is reached only from a terminal: the root it writes to
        is an argument, so no caller can install a change without saying where.
        An existing version is never overwritten; a request whose version is
        already there is refused and stays open. The target directory is
        claimed with `os.mkdir` before anything is copied, so of two requests
        for one version exactly one wins, and when the record cannot be written
        the files this call copied are removed and the request can be applied
        again.
        """
        actor = require_actor(by, "applying a change request records who applied it")
        request = self.get(request_id)
        require_open(request)
        source = self.directory(request_id) / request.package
        if not source.is_dir():
            raise ChangeRequestError(f"change request {request_id} holds no package")
        claim = claim_target(Path(catalog_root) / request.package)
        target = claim.target
        try:
            shutil.copytree(source, target, dirs_exist_ok=True)
            request.state = ChangeRequestState.applied
            request.decided_at = datetime.now(UTC)
            request.decided_by = actor
            request.applied_to = str(Path(catalog_root))
            self._write(request)
        except BaseException:
            claim.release()
            raise
        return sorted(path for path in target.rglob("*") if path.is_file())

    def reject(self, request_id: str, *, by: str, note: str = "") -> ChangeRequest:
        """Close one open request without copying anything, recording who and why."""
        actor = require_actor(by, "rejecting a change request records who rejected it")
        request = self.get(request_id)
        require_open(request)
        request.state = ChangeRequestState.rejected
        request.decided_at = datetime.now(UTC)
        request.decided_by = actor
        request.note = note
        self._write(request)
        return request

    def _write(self, request: ChangeRequest) -> None:
        directory = self.directory(request.request_id)
        directory.mkdir(parents=True, exist_ok=True)
        payload = request.model_dump(mode="json")
        (directory / REQUEST_FILENAME).write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        (directory / SUMMARY_FILENAME).write_text(summarize(request), encoding="utf-8")


def summarize(request: ChangeRequest) -> str:
    """What changes and who asked, in the order a reviewer reads it."""
    issues = request.quality.issues
    lines = [
        f"# Change request {request.request_id}",
        "",
        f"{request.actor} asks for {request.skill_id} at version {request.version}.",
        "",
        f"- Requested at: {request.created_at.isoformat()}",
        f"- Drafted from proposal: {request.proposal_id}",
        f"- Package: {request.package}",
        f"- Quality score: {request.quality.score:.2f}",
        f"- Quality issues: {'; '.join(issues) if issues else 'none'}",
        f"- State: {request.state.value}",
    ]
    if request.decided_at is not None:
        lines.append(
            f"- Decided at: {request.decided_at.isoformat()} by {request.decided_by}"
        )
    if request.applied_to:
        lines.append(f"- Applied into: {request.applied_to}")
    if request.branch:
        where = "pushed" if request.pushed else "not pushed"
        lines.append(
            f"- Branch: {request.branch} at {request.commit} in {request.repository} ({where})"
        )
    if request.pull_request:
        lines.append(f"- Pull request: {request.pull_request}")
    if request.note:
        lines.append(f"- Note: {request.note}")
    lines.extend(
        [
            "",
            "This package is a proposal. Nothing serves it until a person copies it",
            "into a catalog root and the runtime is pointed at a catalog built from",
            "that root. Applying it is that person's act, and it is recorded here.",
            "",
        ]
    )
    return "\n".join(lines)


def store_for(settings: Settings) -> ChangeRequestStore:
    """The directory store these settings configure.

    A deployment on another storage backend opens its store with
    `oska.services.open_changes`, which picks the backend the configuration names.
    """
    runtime = load_runtime_config(settings=settings)
    return ChangeRequestStore(state_path(Path(settings.state_dir), runtime.changes.root))
