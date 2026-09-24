"""Generating a pack or a platform from the templates shipped with the package.

A template is a real file under `src/oska/templates/`, on disk exactly as it
would read in a generated pack, `${placeholder}` and all. Generating a pack or
a platform is walking that tree and substituting every placeholder in every
file's name and content with `string.Template.safe_substitute`: `safe`
because a skill file legitimately carries its own `$payload.field` and
`$service`-style tokens (read by the mock connector and by an operation's
`verify.expect`), and those must survive untouched rather than raise on a name
the mapping does not define.

`new_pack` writes one pack, ready to pass `oska pack check` as generated. Given
a platform manifest, it also names the new pack there and adds it to the
manifest's default domain, so the platform serves it with no edit by hand; the
manifest is rewritten as readable YAML with its leading comment kept, and any
other comment in it is lost. `enclosing_platform` is how a caller finds that
manifest when it writes a pack into the `packs/` directory of the platform it
runs in.
`new_platform` writes a platform repository around one or more existing packs,
copying each pack into it so the result is self-contained. Its `configs/` is
not a template: it is a copy of the defaults the package ships, taken at
generation time, so there is one copy of those defaults to keep current.
"""

from __future__ import annotations

import re
import shutil
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
from string import Template

import yaml

from oska.config import PACKAGED_CONFIGS
from oska.contracts.platform import (
    DomainSpec,
    EnvironmentSpec,
    HostIdentity,
    PackRef,
    PlatformManifest,
)
from oska.platform.errors import PackError
from oska.platform.packs import load_pack

__all__ = [
    "DEFAULT_ENTITY",
    "DEFAULT_HOST",
    "DEFAULT_SYSTEM",
    "PACKS_DIR",
    "PLATFORM_MANIFEST",
    "enclosing_platform",
    "new_pack",
    "new_platform",
    "register_pack",
]

# Mirrors PackManifest.name / PlatformManifest.name in oska.contracts.platform.
_NAME = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")
# Mirrors the identifier segment of a skill id in oska.contracts.skill.
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")

DEFAULT_ENTITY = "item"
DEFAULT_SYSTEM = "core"
DEFAULT_HOST = "Assistant"
_DEFAULT_DOMAIN = "default"
_CONFIGS_DIR = "configs"
PLATFORM_MANIFEST = "platform.yaml"
PACKS_DIR = "packs"


def new_pack(
    name: str,
    *,
    entity: str = DEFAULT_ENTITY,
    system: str = DEFAULT_SYSTEM,
    host: str = DEFAULT_HOST,
    into: Path | str | None = None,
    platform: Path | str | None = None,
) -> Path:
    """Write the `blank` pack template as a new pack, substituted for this business area.

    With `platform`, the pack is also named in that manifest and added to its
    default domain; the manifest is checked before anything is written.
    """
    _check_name(name, "pack name")
    _check_identifier(entity, "entity")
    _check_identifier(system, "system")
    if not host.strip():
        raise ValueError("a host name cannot be empty")
    if platform is not None:
        _registration(Path(platform), name)
    target = _target(name, into)
    mapping = {
        "pack": name,
        "entity": entity,
        "Entity": entity[:1].upper() + entity[1:],
        "entities": _pluralize(entity),
        "system": system,
        "host": host,
    }
    _render_tree(_template("pack", "blank"), target, mapping)
    if platform is not None:
        register_pack(platform, target)
    return target


def register_pack(platform: Path | str, pack_dir: Path | str) -> str:
    """Name a pack in a platform manifest and add it to the default domain; return that domain.

    The pack's path is written relative to the manifest when the pack is under
    it. A name the manifest already holds, or a manifest that would not hold up
    afterwards, is refused and the file is left as it was.
    """
    path = Path(platform)
    try:
        pack = load_pack(Path(pack_dir).resolve())
    except PackError as exc:
        raise ValueError(f"{pack_dir}: not a usable pack: {exc}") from exc
    text, document, domain = _registration(path, pack.name)
    base = path.resolve().parent
    location = (
        pack.root.relative_to(base).as_posix() if pack.root.is_relative_to(base) else str(pack.root)
    )
    document["packs"] = {**(document.get("packs") or {}), pack.name: {"path": location}}
    domain["packs"] = [*(domain.get("packs") or []), pack.name]
    try:
        PlatformManifest.model_validate(document)
    except ValueError as exc:
        raise ValueError(f"{path}: adding pack {pack.name} does not hold up: {exc}") from exc
    path.write_text(_leading_comments(text) + _dump(document), encoding="utf-8")
    return str(domain["name"])


def enclosing_platform(into: Path | str | None, *, cwd: Path | str | None = None) -> Path | None:
    """The manifest of the platform a pack is written into, when it is written into its `packs/`.

    That is the `platform.yaml` of the working directory, when there is one and
    `into` is that directory's `packs/`; otherwise there is none.
    """
    here = Path(cwd) if cwd is not None else Path.cwd()
    manifest = here / PLATFORM_MANIFEST
    if into is None or not manifest.is_file():
        return None
    if Path(into).resolve() != (here / PACKS_DIR).resolve():
        return None
    return manifest


def new_platform(
    name: str,
    *,
    packs: Path | str | list[Path | str] = (),
    into: Path | str | None = None,
) -> Path:
    """Write a platform repository around one or more existing packs.

    Each pack is copied under `packs/<its name>/` so the result is a
    self-contained repository, and `platform.yaml` names them in one domain.
    Building the manifest through `PlatformManifest` rather than a static
    template is deliberate: the packs and the domain they form are given on
    the call, of a length a template cannot fix in advance, while every other
    file of this template is the same for every platform and is rendered the
    usual way.
    """
    _check_name(name, "platform name")
    sources = [packs] if isinstance(packs, (str, Path)) else list(packs)
    target = _target(name, into)
    display_host = name.replace("_", " ").replace("-", " ").title() or DEFAULT_HOST
    example_pack = _pack_display_name(sources[0]) if sources else "your_pack"
    mapping = {"platform": name, "host": display_host, "pack": example_pack}
    _render_tree(_template("platform"), target, mapping)
    shutil.copytree(PACKAGED_CONFIGS, target / _CONFIGS_DIR)
    pack_names = [_copy_pack(source, target) for source in sources]
    manifest = PlatformManifest(
        name=name,
        packs={
            pack_name: PackRef(path=Path("packs") / pack_name) for pack_name in pack_names
        },
        domains=[
            DomainSpec(
                name=_DEFAULT_DOMAIN,
                packs=pack_names,
                host=HostIdentity(name=display_host),
            )
        ],
        default_domain=_DEFAULT_DOMAIN,
    )
    _write_yaml(target / "platform.yaml", manifest)
    return target


def _copy_pack(source: Path | str, target: Path) -> str:
    """Copy one pack into the platform's `packs/` directory, and return its name."""
    origin = Path(source).resolve()
    try:
        pack = load_pack(origin)
    except PackError as exc:
        raise ValueError(f"{origin}: not a usable pack: {exc}") from exc
    destination = target / "packs" / pack.name
    if destination.exists():
        raise FileExistsError(f"{destination} already exists")
    shutil.copytree(pack.root, destination)
    return pack.name


def _pack_display_name(source: Path | str) -> str:
    try:
        return load_pack(Path(source).resolve()).name
    except PackError:
        return Path(source).name


def _write_yaml(path: Path, manifest: PlatformManifest) -> None:
    document = manifest.model_dump(mode="json", exclude_none=True)
    if not document.get("tenants"):
        document.pop("tenants", None)
    for domain in document.get("domains", []):
        if domain.get("environment") == EnvironmentSpec().model_dump(mode="json"):
            domain.pop("environment")
    path.write_text(_dump(document), encoding="utf-8")


def _dump(document: dict) -> str:
    return yaml.safe_dump(document, sort_keys=False, default_flow_style=False, allow_unicode=True)


def _registration(path: Path, name: str) -> tuple[str, dict, dict]:
    """A manifest's text, its document, and the default domain a pack named `name` joins."""
    try:
        text = path.read_text(encoding="utf-8")
        document = yaml.safe_load(text)
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"{path}: cannot be read: {exc}") from exc
    if not isinstance(document, dict):
        raise ValueError(f"{path}: expected a mapping")
    if name in (document.get("packs") or {}):
        raise ValueError(f"{path} already names a pack {name}")
    domains = [domain for domain in document.get("domains") or [] if isinstance(domain, dict)]
    wanted = document.get("default_domain") or (domains[0].get("name") if domains else None)
    for domain in domains:
        if domain.get("name") == wanted:
            return text, document, domain
    raise ValueError(f"{path}: declares no default domain for pack {name} to join")


def _leading_comments(text: str) -> str:
    """The comment lines that open a file, kept when the rest of it is rewritten."""
    lines: list[str] = []
    for line in text.splitlines(keepends=True):
        if not line.startswith("#"):
            break
        lines.append(line)
    return "".join(lines)


def _target(name: str, into: Path | str | None) -> Path:
    base = Path(into) if into is not None else Path.cwd()
    target = base / name
    if target.exists():
        raise FileExistsError(f"{target} already exists")
    return target


def _template(*parts: str) -> Traversable:
    root = resources.files("oska.templates")
    for part in parts:
        root = root / part
    return root


def _render_tree(source: Traversable, target: Path, mapping: dict[str, str]) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for entry in sorted(source.iterdir(), key=lambda item: item.name):
        name = Template(entry.name).safe_substitute(mapping)
        destination = target / name
        if entry.is_dir():
            _render_tree(entry, destination, mapping)
            continue
        text = entry.read_text(encoding="utf-8")
        destination.write_text(Template(text).safe_substitute(mapping), encoding="utf-8")


def _check_name(value: str, what: str) -> None:
    if not _NAME.match(value):
        raise ValueError(f"{what} {value!r} must be lowercase letters, digits, - and _")


def _check_identifier(value: str, what: str) -> None:
    if not _IDENTIFIER.match(value):
        raise ValueError(f"{what} {value!r} must be lowercase letters, digits and _ only")


def _pluralize(word: str) -> str:
    if word.endswith(("s", "x", "z", "ch", "sh")):
        return f"{word}es"
    if word.endswith("y") and word[-2:-1] not in "aeiou":
        return f"{word[:-1]}ies"
    return f"{word}s"
