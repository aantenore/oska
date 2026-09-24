"""Reading `platform.yaml`: what an organization deploys, with its packs located.

The manifest names packs by path. A relative path is read from the directory
of the manifest, not from wherever the process was started, so a platform
directory can be moved or checked out anywhere and still find its packs.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from oska.contracts.platform import PlatformManifest
from oska.platform.errors import ManifestError

__all__ = ["load_platform"]


def load_platform(path: Path | str) -> PlatformManifest:
    """Read and check a platform manifest, resolving its pack paths."""
    target = Path(path)
    try:
        document = yaml.safe_load(target.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ManifestError(f"{target}: cannot be read: {exc}") from exc
    if not isinstance(document, dict):
        raise ManifestError(f"{target}: expected a mapping")
    try:
        manifest = PlatformManifest.model_validate(document)
    except ValidationError as exc:
        raise ManifestError(f"{target}: {_messages(exc)}") from exc
    base = target.resolve().parent
    for ref in manifest.packs.values():
        if not ref.path.is_absolute():
            ref.path = (base / ref.path).resolve()
    return manifest


def _messages(exc: ValidationError) -> str:
    """Every problem pydantic found, one clause each."""
    parts = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error.get("loc", ()))
        message = str(error.get("msg", "invalid value")).removeprefix("Value error, ")
        parts.append(f"{location}: {message}" if location else message)
    return "; ".join(parts) or str(exc)
