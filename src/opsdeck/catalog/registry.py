"""In-memory catalog that reloads when the files on disk change."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from pathlib import Path

from opsdeck.catalog.loader import (
    SkillParseError,
    discover_all,
    fill_composite_capabilities,
    load_skill,
)
from opsdeck.catalog.validator import ValidationIssue
from opsdeck.contracts.common import Hydrated
from opsdeck.contracts.skill import Skill, SkillType


class SkillRegistry:
    """Every skill under the configured roots, keyed by id.

    Each public call compares a fingerprint of the discovered files (path, size
    and modification time) with the one held; a difference triggers a reload, so
    an edited or added skill is picked up without a restart.
    """

    def __init__(self, roots: Sequence[Path]) -> None:
        self._roots = [Path(root) for root in roots]
        self._fingerprint = ""
        self._skills: list[Skill] = []
        self._current: dict[str, Skill] = {}
        self._errors: list[ValidationIssue] = []
        self._loaded = False

    @property
    def roots(self) -> list[Path]:
        """Roots this registry reads."""
        return list(self._roots)

    def fingerprint(self) -> str:
        """Fingerprint of the files currently on disk."""
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
        paths = discover_all(self._roots)
        fingerprint = _fingerprint(paths)
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
