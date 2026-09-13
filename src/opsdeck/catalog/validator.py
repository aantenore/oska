"""Schema and cross-reference checks over the catalog."""

from __future__ import annotations

import json
from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path
from typing import Literal

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ValidationError

from opsdeck.catalog.loader import composite_capabilities
from opsdeck.config import load_settings
from opsdeck.contracts.policy import Policy
from opsdeck.contracts.skill import Skill, SkillType

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


class CatalogError(Exception):
    """A catalog that carries an error, raised where it would otherwise be served.

    The issues travel on the exception, so a caller reports what was found
    instead of running the validator again to find out.
    """

    def __init__(self, issues: Sequence[ValidationIssue]) -> None:
        self.issues = list(issues)
        super().__init__("; ".join(str(issue) for issue in self.issues))


def validate(
    skill: Skill,
    all_skills: Sequence[Skill],
    *,
    schema_dir: Path | None = None,
) -> list[ValidationIssue]:
    """Check one skill against the schema and against the rest of the catalog."""
    directory = schema_dir or load_settings().schemas_dir
    issues: list[ValidationIssue] = []
    issues.extend(_schema_issues(skill, directory))
    by_id = _current_by_id(all_skills)
    issues.extend(_reference_issues(skill, by_id))
    issues.extend(_capability_issues(skill, by_id))
    issues.extend(_operation_issues(skill))
    issues.extend(_ontology_issues(skill, by_id))
    issues.extend(_runtime_issues(skill))
    issues.extend(_policy_issues(skill, directory))
    return issues


def validate_all(
    all_skills: Sequence[Skill],
    *,
    schema_dir: Path | None = None,
) -> list[ValidationIssue]:
    """Check every skill in the catalog."""
    return [
        issue
        for skill in all_skills
        for issue in validate(skill, all_skills, schema_dir=schema_dir)
    ]


def schema_validator(schema_dir: Path, name: str) -> Draft202012Validator:
    """Return the validator for one bundled schema."""
    return _load_validator(str(Path(schema_dir) / name))


def _schema_issues(skill: Skill, schema_dir: Path) -> list[ValidationIssue]:
    payload = skill.frontmatter.model_dump(mode="json", exclude_none=True)
    validator = schema_validator(schema_dir, SKILL_SCHEMA)
    return [
        _issue(skill, "schema", f"{_pointer(error.absolute_path)}{error.message}")
        for error in validator.iter_errors(payload)
    ]


def _reference_issues(skill: Skill, by_id: dict[str, Skill]) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    checks = (
        (skill.frontmatter.entities, SkillType.entity),
        (skill.frontmatter.integrations, SkillType.integration),
        (skill.frontmatter.runbooks, SkillType.runbook),
    )
    for references, expected in checks:
        for reference in references:
            target = by_id.get(reference)
            if target is None:
                issues.append(
                    _issue(skill, "unknown_reference", f"{reference} is not in the catalog")
                )
                continue
            if target.type is not expected:
                issues.append(
                    _issue(
                        skill,
                        "wrong_reference_type",
                        f"{reference} is a {target.type.value}, expected {expected.value}",
                    )
                )
            elif not target.is_published:
                issues.append(
                    _issue(skill, "unpublished_reference", f"{reference} is not published")
                )
    return issues


def _capability_issues(skill: Skill, by_id: dict[str, Skill]) -> list[ValidationIssue]:
    frontmatter = skill.frontmatter
    if frontmatter.type is SkillType.composite:
        expected = composite_capabilities(frontmatter.runbooks, by_id)
        declared = frontmatter.capabilities
        if sorted(declared.reads) != sorted(expected.reads) or sorted(declared.writes) != sorted(
            expected.writes
        ):
            return [
                _issue(
                    skill,
                    "capability_mismatch",
                    "capabilities must equal the union of the capabilities of its runbooks",
                )
            ]
        return []
    declared_ops = {
        spec.name: spec
        for integration in frontmatter.integrations
        for spec in _operations_of(by_id.get(integration))
    }
    issues: list[ValidationIssue] = []
    for name in frontmatter.capabilities.reads:
        spec = declared_ops.get(name)
        if spec is None:
            issues.append(
                _issue(skill, "undeclared_capability", f"{name} is not declared by an integration")
            )
        elif spec.write:
            issues.append(_issue(skill, "read_is_write", f"{name} is a write operation"))
    for name in frontmatter.capabilities.writes:
        spec = declared_ops.get(name)
        if spec is None:
            issues.append(
                _issue(skill, "undeclared_capability", f"{name} is not declared by an integration")
            )
        elif not spec.write:
            issues.append(_issue(skill, "write_is_read", f"{name} is not a write operation"))
    return issues


def _operation_issues(skill: Skill) -> list[ValidationIssue]:
    if skill.type is not SkillType.integration:
        return []
    issues: list[ValidationIssue] = []
    reads = {spec.name for spec in skill.frontmatter.operations if not spec.write}
    for spec in skill.frontmatter.operations:
        if not spec.write:
            continue
        if spec.verify is None:
            issues.append(_issue(skill, "missing_verify", f"{spec.name} declares no read-back"))
            continue
        if spec.verify.operation not in reads:
            issues.append(
                _issue(
                    skill,
                    "unknown_verify_operation",
                    f"{spec.name}: {spec.verify.operation} is not a read operation here",
                )
            )
        for field in spec.idempotency_fields:
            if field not in _properties(spec.input_schema):
                issues.append(
                    _issue(
                        skill,
                        "unknown_idempotency_field",
                        f"{spec.name}: {field} is not in the input schema",
                    )
                )
    return issues


def _ontology_issues(skill: Skill, by_id: dict[str, Skill]) -> list[ValidationIssue]:
    if skill.type is SkillType.entity:
        return _entity_ontology_issues(skill, by_id)
    if skill.type is SkillType.integration:
        return _binding_issues(skill, by_id)
    return []


def _entity_ontology_issues(skill: Skill, by_id: dict[str, Skill]) -> list[ValidationIssue]:
    frontmatter = skill.frontmatter
    issues: list[ValidationIssue] = []
    if frontmatter.state_field is not None:
        if frontmatter.field(frontmatter.state_field) is None:
            issues.append(
                _issue(
                    skill,
                    "ontology_unknown_state_field",
                    f"{frontmatter.state_field} is not a declared field",
                )
            )
        if not frontmatter.states:
            issues.append(
                _issue(
                    skill,
                    "ontology_missing_states",
                    f"{frontmatter.state_field} declares no allowed values",
                )
            )
    elif frontmatter.states:
        issues.append(
            _issue(skill, "ontology_missing_states", "states are declared without a state field")
        )
    for relation in frontmatter.relations:
        target = by_id.get(relation.target)
        if target is None or target.type is not SkillType.entity:
            issues.append(
                _issue(
                    skill,
                    "ontology_unknown_relation_target",
                    f"relation {relation.name}: {relation.target} is not an entity in the catalog",
                )
            )
        elif not target.is_published:
            issues.append(
                _issue(
                    skill,
                    "ontology_unpublished_relation_target",
                    f"relation {relation.name}: {relation.target} is not published",
                )
            )
    return issues


def _binding_issues(skill: Skill, by_id: dict[str, Skill]) -> list[ValidationIssue]:
    frontmatter = skill.frontmatter
    listed = set(frontmatter.entities)
    issues: list[ValidationIssue] = []
    for spec in frontmatter.operations:
        if spec.entity is None:
            if skill.is_published:
                issues.append(
                    _issue(
                        skill,
                        "ontology_unbound_operation",
                        f"{spec.name}: an operation is bound to exactly one entity type",
                    )
                )
        elif spec.entity not in listed:
            issues.append(
                _issue(
                    skill,
                    "ontology_unlisted_operation_entity",
                    f"{spec.name}: {spec.entity} is not one of the entities this integration lists",
                )
            )
        if not spec.transitions:
            continue
        entity = by_id.get(spec.entity) if spec.entity else None
        if entity is None or entity.type is not SkillType.entity:
            issues.append(
                _issue(
                    skill,
                    "ontology_transition_without_entity",
                    f"{spec.name}: transitions need an entity that is in the catalog",
                )
            )
            continue
        state_field = entity.frontmatter.state_field
        for key, value in spec.transitions.items():
            if state_field is None or key != state_field:
                issues.append(
                    _issue(
                        skill,
                        "ontology_unknown_transition_field",
                        f"{spec.name}: {key} is not the state field of {entity.id}",
                    )
                )
                continue
            if value not in entity.frontmatter.states:
                issues.append(
                    _issue(
                        skill,
                        "ontology_unknown_transition_state",
                        f"{spec.name}: {value} is not a declared state of {entity.id}",
                    )
                )
    return issues


def _runtime_issues(skill: Skill) -> list[ValidationIssue]:
    if skill.type not in (SkillType.runbook, SkillType.composite):
        return []
    issues: list[ValidationIssue] = []
    if skill.frontmatter.runtime is None:
        issues.append(_issue(skill, "missing_runtime", "no runtime wording declared"))
    if len(skill.frontmatter.triggers) < 3:
        issues.append(_issue(skill, "too_few_triggers", "at least three triggers are required"))
    if len(set(skill.frontmatter.triggers)) != len(skill.frontmatter.triggers):
        issues.append(_issue(skill, "duplicate_triggers", "triggers must be distinct"))
    return issues


def _policy_issues(skill: Skill, schema_dir: Path) -> list[ValidationIssue]:
    frontmatter = skill.frontmatter
    if frontmatter.type is SkillType.composite:
        if frontmatter.policy:
            return [
                _issue(
                    skill,
                    "unexpected_policy",
                    "a composite runs each runbook under its own policy",
                )
            ]
        return []
    if frontmatter.policy is None:
        if frontmatter.capabilities.writes:
            return [_issue(skill, "missing_policy", "a procedure that writes needs a policy")]
        return []
    path = (skill.path.parent / frontmatter.policy).resolve()
    if not path.is_file():
        return [_issue(skill, "missing_policy_file", f"{frontmatter.policy} does not exist")]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [_issue(skill, "invalid_policy_file", f"{frontmatter.policy}: {exc}")]
    issues = [
        _issue(skill, "policy_schema", f"{_pointer(error.absolute_path)}{error.message}")
        for error in schema_validator(schema_dir, POLICY_SCHEMA).iter_errors(payload)
    ]
    if issues:
        return issues
    try:
        policy = Policy.model_validate(payload)
    except ValidationError as exc:
        return [_issue(skill, "invalid_policy", str(exc))]
    if policy.skill_id != skill.id:
        issues.append(
            _issue(skill, "policy_skill_mismatch", f"policy names {policy.skill_id}")
        )
    declared = set(frontmatter.capabilities.writes)
    for rule in policy.rules:
        if rule.operation == "*":
            continue
        if rule.operation not in declared and rule.operation not in frontmatter.capabilities.reads:
            issues.append(
                _issue(
                    skill,
                    "policy_undeclared_operation",
                    f"{rule.operation} is not a capability of this procedure",
                )
            )
    return issues


def _operations_of(skill: Skill | None) -> list:
    return list(skill.frontmatter.operations) if skill is not None else []


def _properties(schema: dict) -> set[str]:
    properties = schema.get("properties")
    return set(properties) if isinstance(properties, dict) else set()


def _current_by_id(skills: Sequence[Skill]) -> dict[str, Skill]:
    current: dict[str, Skill] = {}
    for skill in skills:
        held = current.get(skill.id)
        if held is None or _rank(skill) > _rank(held):
            current[skill.id] = skill
    return current


def _rank(skill: Skill) -> tuple[int, int]:
    return (1 if skill.is_published else 0, skill.version)


def _pointer(path) -> str:
    parts = [str(part) for part in path]
    return f"{'.'.join(parts)}: " if parts else ""


def _issue(skill: Skill, code: str, message: str) -> ValidationIssue:
    return ValidationIssue(skill_id=skill.id, code=code, message=message, path=skill.path)


@lru_cache(maxsize=8)
def _load_validator(path: str) -> Draft202012Validator:
    schema = json.loads(Path(path).read_text(encoding="utf-8"))
    return Draft202012Validator(schema)
