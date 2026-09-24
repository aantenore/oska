"""What every catalog check shares: the issue it reports and the schemas it reads."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Literal

from jsonschema import Draft202012Validator
from pydantic import BaseModel

from oska.contracts.skill import Skill

__all__ = [
    "POLICY_SCHEMA",
    "PROPOSAL_SCHEMA",
    "SKILL_SCHEMA",
    "ValidationIssue",
    "issue",
    "operations_of",
    "pointer",
    "properties",
    "schema_validator",
]

SKILL_SCHEMA = "skill.schema.json"
POLICY_SCHEMA = "policy.schema.json"
PROPOSAL_SCHEMA = "proposal.schema.json"


class ValidationIssue(BaseModel):
    """One problem found in a skill file."""

    skill_id: str
    code: str
    message: str
    severity: Literal["error", "warning"] = "error"
    path: Path | None = None

    def __str__(self) -> str:
        return f"{self.severity}: {self.skill_id or self.path}: {self.code}: {self.message}"


def schema_validator(schema_dir: Path, name: str) -> Draft202012Validator:
    """Return the validator for one bundled schema."""
    return _load_validator(str(Path(schema_dir) / name))


def operations_of(skill: Skill | None) -> list:
    """Every operation a skill declares, or none for a skill that is not there."""
    return list(skill.frontmatter.operations) if skill is not None else []


def properties(schema: dict) -> set[str]:
    """The property names an input schema declares."""
    properties = schema.get("properties")
    return set(properties) if isinstance(properties, dict) else set()


def pointer(path) -> str:
    """Where in a document a schema error was found, as a prefix for its message."""
    parts = [str(part) for part in path]
    return f"{'.'.join(parts)}: " if parts else ""


def issue(skill: Skill, code: str, message: str) -> ValidationIssue:
    """One error found in this skill."""
    return ValidationIssue(skill_id=skill.id, code=code, message=message, path=skill.path)


@lru_cache(maxsize=8)
def _load_validator(path: str) -> Draft202012Validator:
    schema = json.loads(Path(path).read_text(encoding="utf-8"))
    return Draft202012Validator(schema)
