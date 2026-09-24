"""A pack as a content-addressed artifact: a tarball named by its own hash.

Every file under the pack is hashed, the hashes are combined in a stable
order, and the combined hash names both the tarball and the manifest beside
it. Building the same pack twice writes the same hash; changing one byte of
one file changes it.

What is never part of a pack is left out: a name matching `DEFAULT_IGNORE` at
any depth, such as an environment file, a version-control directory, compiled
bytecode or runtime state. A symbolic link refuses the build, because the
archive would hold one thing and the hash would name another.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import tarfile
from collections.abc import Iterable
from pathlib import Path

from pydantic import BaseModel

from oska.platform.errors import PackError
from oska.platform.packs import load_pack

__all__ = ["DEFAULT_IGNORE", "PackArtifact", "build_pack"]

HASH_LENGTH = 16
DEFAULT_IGNORE: tuple[str, ...] = (".env*", ".git", "__pycache__", "*.pyc", "state")
"""Names, matched against every file and directory of a pack, that a build leaves out."""


class PackArtifact(BaseModel):
    """One built pack: its tarball, its manifest, and the hash that names both."""

    pack: str
    version: str
    content_hash: str
    archive_path: Path
    manifest_path: Path
    files: dict[str, str]


def build_pack(
    path: Path | str, into: Path | str, *, ignore: Iterable[str] = DEFAULT_IGNORE
) -> PackArtifact:
    """Write a pack as a tarball named by a hash over its files, plus a manifest listing them.

    A file or directory whose name matches a pattern of `ignore` is left out; a
    symbolic link anywhere else in the pack is refused.
    """
    try:
        pack = load_pack(path)
    except PackError as exc:
        raise ValueError(f"{path}: not a usable pack: {exc}") from exc
    files = _file_hashes(pack.root, tuple(ignore))
    content_hash = _content_hash(files)
    destination = Path(into)
    destination.mkdir(parents=True, exist_ok=True)
    stem = f"{pack.name}-{pack.version}-{content_hash}"
    archive_path = destination / f"{stem}.tar.gz"
    manifest_path = destination / f"{stem}.MANIFEST.json"
    _write_archive(archive_path, pack.root, pack.name, files)
    manifest_path.write_text(
        json.dumps(
            {
                "pack": pack.name,
                "version": pack.version,
                "content_hash": content_hash,
                "files": files,
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return PackArtifact(
        pack=pack.name,
        version=pack.version,
        content_hash=content_hash,
        archive_path=archive_path,
        manifest_path=manifest_path,
        files=files,
    )


def _file_hashes(root: Path, ignore: tuple[str, ...]) -> dict[str, str]:
    """Every file a pack ships, by its path relative to the pack root, in a stable order."""
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(_shipped(root, ignore))
    }


def _shipped(root: Path, ignore: tuple[str, ...]) -> list[Path]:
    """The files under a pack that a build holds, refusing a symbolic link among them."""
    found: list[Path] = []
    for directory, subdirectories, filenames in os.walk(root, followlinks=False):
        base = Path(directory)
        subdirectories[:] = [name for name in subdirectories if not _ignored(name, ignore)]
        for name in [*subdirectories, *filenames]:
            if _ignored(name, ignore):
                continue
            entry = base / name
            if entry.is_symlink():
                raise ValueError(
                    f"{entry.relative_to(root).as_posix()} is a symbolic link; a built pack "
                    "holds files only, so replace the link with the file it points to"
                )
            if entry.is_file():
                found.append(entry)
    return found


def _ignored(name: str, ignore: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in ignore)


def _content_hash(files: dict[str, str]) -> str:
    material = json.dumps(files, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:HASH_LENGTH]


def _write_archive(archive_path: Path, root: Path, pack_name: str, files: dict[str, str]) -> None:
    with tarfile.open(archive_path, "w:gz") as archive:
        for relative in sorted(files):
            info = archive.gettarinfo(root / relative, arcname=f"{pack_name}/{relative}")
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            with (root / relative).open("rb") as handle:
                archive.addfile(info, handle)
