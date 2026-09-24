"""Reading skill files from disk."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Sequence
from pathlib import Path

import yaml
from pydantic import ValidationError

from oska.contracts.skill import Capabilities, Skill, SkillFrontmatter, SkillSections, SkillType

SKILL_FILENAME = "skill.md"
SKILL_GLOB = f"**/v*/{SKILL_FILENAME}"

_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.DOTALL)
_HEADING = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
_KNOWN_SECTIONS = ("purpose", "preconditions", "steps", "verification", "rollback")


class SkillParseError(ValueError):
    """A skill file could not be read."""


def discover(root: Path) -> list[Path]:
    """Return every skill file under a root, sorted. A missing root yields nothing."""
    base = Path(root)
    if not base.is_dir():
        return []
    return sorted(base.glob(SKILL_GLOB))


def discover_all(roots: Iterable[Path]) -> list[Path]:
    """Return every skill file under the given roots, de-duplicated and sorted."""
    seen: dict[Path, None] = {}
    for root in roots:
        for path in discover(root):
            seen.setdefault(path.resolve(), None)
    return sorted(seen)


def load_skill(path: Path) -> Skill:
    """Parse one skill file."""
    target = Path(path)
    try:
        raw = target.read_bytes()
    except OSError as exc:
        raise SkillParseError(f"{target}: cannot be read: {exc}") from exc
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SkillParseError(f"{target}: is not valid UTF-8") from exc
    return parse_skill_text(text, target)


def parse_skill_text(text: str, path: Path) -> Skill:
    """Parse the text of a skill file held in memory.

    The path is not read: it is what the skill reports, and what a relative
    reference such as a policy file resolves against.
    """
    target = Path(path)
    match = _FRONTMATTER.match(text)
    if match is None:
        raise SkillParseError(f"{target}: no YAML frontmatter between --- lines")
    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        raise SkillParseError(f"{target}: invalid YAML frontmatter: {exc}") from exc
    if not isinstance(data, dict):
        raise SkillParseError(f"{target}: frontmatter is not a mapping")
    try:
        frontmatter = SkillFrontmatter.model_validate(data)
    except ValidationError as exc:
        raise SkillParseError(f"{target}: invalid frontmatter: {_first_error(exc)}") from exc
    return Skill(
        frontmatter=frontmatter,
        sections=parse_sections(text[match.end() :]),
        path=target,
        fingerprint=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


def parse_sections(body: str) -> SkillSections:
    """Split a markdown body into its H2 sections."""
    values: dict[str, str] = {}
    extra: dict[str, str] = {}
    headings = list(_HEADING.finditer(body))
    for position, heading in enumerate(headings):
        end = headings[position + 1].start() if position + 1 < len(headings) else len(body)
        title = heading.group(1).strip().lower()
        content = body[heading.end() : end].strip()
        if title in _KNOWN_SECTIONS:
            values[title] = content
        else:
            extra[title] = content
    return SkillSections(**values, extra=extra)


def fill_composite_capabilities(skills: Sequence[Skill]) -> None:
    """Give every composite that omits its capabilities the union of its runbooks'."""
    by_id = _current_by_id(skills)
    for skill in skills:
        frontmatter = skill.frontmatter
        if frontmatter.type is not SkillType.composite:
            continue
        if frontmatter.capabilities.reads or frontmatter.capabilities.writes:
            continue
        frontmatter.capabilities = composite_capabilities(frontmatter.runbooks, by_id)


def composite_capabilities(runbook_ids: Sequence[str], by_id: dict[str, Skill]) -> Capabilities:
    """Return the union of the capabilities of the given runbooks, in first-seen order."""
    reads: dict[str, None] = {}
    writes: dict[str, None] = {}
    for runbook_id in runbook_ids:
        member = by_id.get(runbook_id)
        if member is None:
            continue
        for name in member.frontmatter.capabilities.reads:
            reads.setdefault(name, None)
        for name in member.frontmatter.capabilities.writes:
            writes.setdefault(name, None)
    return Capabilities(reads=list(reads), writes=list(writes))


def _current_by_id(skills: Sequence[Skill]) -> dict[str, Skill]:
    current: dict[str, Skill] = {}
    for skill in skills:
        held = current.get(skill.id)
        if held is None or _rank(skill) > _rank(held):
            current[skill.id] = skill
    return current


def _rank(skill: Skill) -> tuple[int, int]:
    return (1 if skill.is_published else 0, skill.version)


def _first_error(exc: ValidationError) -> str:
    errors = exc.errors()
    if not errors:
        return str(exc)
    first = errors[0]
    location = ".".join(str(part) for part in first.get("loc", ()))
    return f"{location}: {first.get('msg', '')}".strip(": ")
