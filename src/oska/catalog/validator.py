"""Schema and cross-reference checks over the catalog.

A skill is checked against the schema, against the rest of the catalog, and
against the configuration the deployment runs under. The last of those is what
the guardrail rule reads: the identifiers a skill declares have to be defined by
the guardrail catalog this installation configures, so a promise nothing keeps
is refused where a catalog is published rather than in the middle of a turn.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path
from typing import Literal

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ValidationError

from oska.catalog.loader import composite_capabilities
from oska.config import Settings, guardrail_catalog_path, load_runtime_config, load_settings
from oska.contracts.policy import Policy
from oska.contracts.skill import OperationSpec, Skill, SkillFrontmatter, SkillType

GUARDRAIL_CATALOG_KEY = "guardrails"

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
    settings: Settings | None = None,
    guardrail_ids: frozenset[str] | None = None,
) -> list[ValidationIssue]:
    """Check one skill against the schema and against the rest of the catalog.

    `guardrail_ids` is what the guardrail catalog defines; without it the
    configuration this installation runs under is read to find out.
    """
    directory = schema_dir or load_settings().schemas_dir
    declared = (
        declared_guardrail_ids(settings) if guardrail_ids is None else frozenset(guardrail_ids)
    )
    issues: list[ValidationIssue] = []
    issues.extend(_schema_issues(skill, directory))
    by_id = _current_by_id(all_skills)
    issues.extend(_reference_issues(skill, by_id))
    issues.extend(_capability_issues(skill, by_id))
    issues.extend(_principal_input_issues(skill, by_id))
    issues.extend(_operation_issues(skill))
    issues.extend(_ontology_issues(skill, by_id))
    issues.extend(_runtime_issues(skill))
    issues.extend(_policy_issues(skill, directory))
    issues.extend(_guardrail_issues(skill, declared))
    return issues


def validate_all(
    all_skills: Sequence[Skill],
    *,
    schema_dir: Path | None = None,
    settings: Settings | None = None,
) -> list[ValidationIssue]:
    """Check every skill in the catalog."""
    declared = declared_guardrail_ids(settings)
    return [
        issue
        for skill in all_skills
        for issue in validate(
            skill, all_skills, schema_dir=schema_dir, guardrail_ids=declared
        )
    ]


def declared_guardrail_ids(settings: Settings | None = None) -> frozenset[str]:
    """The identifiers the configured guardrail catalog defines.

    A deployment that configures no catalog, or names one that is not on disk,
    defines none, and a skill that declares an identifier there is declaring
    something nothing can keep. Reading this file is all the catalog layer does
    with it; what each entry means, and what enforces it, belongs above.
    """
    try:
        resolved = settings or load_settings()
        path = guardrail_catalog_path(
            load_runtime_config(settings=resolved).guardrails, resolved
        )
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return frozenset()
    entries = document.get(GUARDRAIL_CATALOG_KEY) if isinstance(document, dict) else None
    return frozenset(entries) if isinstance(entries, dict) else frozenset()


def schema_validator(schema_dir: Path, name: str) -> Draft202012Validator:
    """Return the validator for one bundled schema."""
    return _load_validator(str(Path(schema_dir) / name))


def _schema_issues(skill: Skill, schema_dir: Path) -> list[ValidationIssue]:
    payload = skill.frontmatter.model_dump(mode="json", exclude_none=True, by_alias=True)
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


def _principal_input_issues(skill: Skill, by_id: dict[str, Skill]) -> list[ValidationIssue]:
    """Every field taken from the identity is a field some operation here accepts.

    A procedure that binds a field nothing accepts has bound nothing: the value
    would be resolved on every turn and written into no payload, and the write
    would go out carrying whatever the conversation supplied instead, which is
    the situation the declaration exists to end. The other check restates in the
    catalog what the contract already refuses, so a frontmatter that reached the
    catalog without passing through it is still caught here.
    """
    frontmatter = skill.frontmatter
    declared = frontmatter.principal_inputs
    if not declared:
        return []
    accepted = _accepted_fields(frontmatter, by_id)
    human = set(frontmatter.human_inputs)
    issues: list[ValidationIssue] = []
    for name in sorted(declared):
        if name in human:
            issues.append(
                _issue(
                    skill,
                    "principal_input_shadows_human_input",
                    f"{name} is declared a human input as well, and a field is either "
                    "asked of the person or taken from the identity",
                )
            )
        if name not in accepted:
            issues.append(
                _issue(
                    skill,
                    "principal_input_not_declared",
                    f"{name} is not a field any operation of this procedure accepts",
                )
            )
    return issues


def _accepted_fields(frontmatter: SkillFrontmatter, by_id: dict[str, Skill]) -> set[str]:
    """Every payload field the operations reachable from this procedure accept.

    A composite declares no integrations of its own, so the operations of its
    runbooks are what it may fill.
    """
    integrations = list(frontmatter.integrations)
    if frontmatter.type is SkillType.composite:
        for member in frontmatter.runbooks:
            held = by_id.get(member)
            if held is not None:
                integrations.extend(held.frontmatter.integrations)
    names: set[str] = set()
    for integration in integrations:
        for spec in _operations_of(by_id.get(integration)):
            names |= _properties(spec.input_schema)
    return names


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
    issues.extend(_lifecycle_issues(skill, by_id))
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


def _lifecycle_issues(skill: Skill, by_id: dict[str, Skill]) -> list[ValidationIssue]:
    """Everything a declared lifecycle has to hold for the runtime to enforce it."""
    frontmatter = skill.frontmatter
    if not frontmatter.lifecycle:
        return []
    issues: list[ValidationIssue] = []
    states = list(frontmatter.states)
    if not frontmatter.state_field or not states:
        issues.append(
            _issue(
                skill,
                "ontology_lifecycle_without_states",
                "a lifecycle needs a state field and the states it may hold",
            )
        )
    issues.extend(_named_state_issues(skill, states))
    if frontmatter.initial_state is None:
        issues.append(
            _issue(
                skill,
                "ontology_lifecycle_without_initial_state",
                "a lifecycle needs the state an entity starts in",
            )
        )
    issues.extend(_move_operation_issues(skill, by_id))
    issues.extend(_state_read_issues(skill, by_id))
    issues.extend(_final_state_issues(skill))
    issues.extend(_reachability_issues(skill))
    issues.extend(_required_move_issues(skill))
    return issues


def _named_state_issues(skill: Skill, states: list[str]) -> list[ValidationIssue]:
    """Every state a lifecycle names is one the entity declares."""
    frontmatter = skill.frontmatter
    named: list[tuple[str, str]] = []
    if frontmatter.initial_state is not None:
        named.append(("the initial state", frontmatter.initial_state))
    named.extend(("a final state", state) for state in frontmatter.final_states)
    for move in frontmatter.lifecycle:
        named.extend((f"a move to {move.to}", origin) for origin in move.from_states)
        named.append((f"the move caused by {move.operation}", move.to))
    return [
        _issue(
            skill,
            "ontology_unknown_lifecycle_state",
            f"{where}: {state} is not one of the declared states",
        )
        for where, state in named
        if state not in states
    ]


def _move_operation_issues(skill: Skill, by_id: dict[str, Skill]) -> list[ValidationIssue]:
    """Every move names a write an integration declares against this entity."""
    bound = _bound_operations(skill.id, by_id)
    issues: list[ValidationIssue] = []
    for move in skill.frontmatter.lifecycle:
        spec = bound.get(move.operation)
        if spec is None:
            issues.append(
                _issue(
                    skill,
                    "ontology_unknown_lifecycle_operation",
                    f"{move.operation} is not an operation bound to {skill.id}",
                )
            )
        elif not spec.write:
            issues.append(
                _issue(
                    skill,
                    "ontology_unknown_lifecycle_operation",
                    f"{move.operation} does not write, so it moves nothing",
                )
            )
    return issues


def _state_read_issues(skill: Skill, by_id: dict[str, Skill]) -> list[ValidationIssue]:
    """The state is read back, so a lifecycle names the read that does it."""
    frontmatter = skill.frontmatter
    if frontmatter.state_read is None:
        return [
            _issue(
                skill,
                "ontology_missing_state_read",
                "a lifecycle names the read the current state is taken from",
            )
        ]
    spec = _bound_operations(skill.id, by_id).get(frontmatter.state_read)
    if spec is None:
        return [
            _issue(
                skill,
                "ontology_unknown_state_read",
                f"{frontmatter.state_read} is not an operation bound to {skill.id}",
            )
        ]
    if spec.write:
        return [
            _issue(
                skill,
                "ontology_unknown_state_read",
                f"{frontmatter.state_read} is a write, so it cannot report the state",
            )
        ]
    return []


def _final_state_issues(skill: Skill) -> list[ValidationIssue]:
    """A final state is one the entity never leaves."""
    final = set(skill.frontmatter.final_states)
    return [
        _issue(
            skill,
            "ontology_move_from_final_state",
            f"{origin} is a final state, so {move.operation} cannot move out of it",
        )
        for move in skill.frontmatter.lifecycle
        for origin in move.from_states
        if origin in final
    ]


def _reachability_issues(skill: Skill) -> list[ValidationIssue]:
    """Every state a move leads to can be arrived at from the initial state.

    A state that only ever appears as a source is an entry the system of record
    supplies, and is not reported: what is reported is a destination nothing
    leads to, because nothing in the catalog could ever put the entity there.
    """
    frontmatter = skill.frontmatter
    initial = frontmatter.initial_state
    if initial is None:
        return []
    reached = {initial}
    changed = True
    while changed:
        changed = False
        for move in frontmatter.lifecycle:
            if move.to not in reached and reached.intersection(move.from_states):
                reached.add(move.to)
                changed = True
    return [
        _issue(
            skill,
            "ontology_unreachable_state",
            f"{state} cannot be reached from {initial}",
        )
        for state in sorted({move.to for move in frontmatter.lifecycle} - reached)
    ]


def _required_move_issues(skill: Skill) -> list[ValidationIssue]:
    """One state, one move that completes the object."""
    counted: dict[str, int] = {}
    for move in skill.frontmatter.lifecycle:
        if not move.required:
            continue
        for origin in move.from_states:
            counted[origin] = counted.get(origin, 0) + 1
    return [
        _issue(
            skill,
            "ontology_conflicting_required_move",
            f"{state} declares {count} required moves, and an entity needs at most one",
        )
        for state, count in sorted(counted.items())
        if count > 1
    ]


def _bound_operations(entity_id: str, by_id: dict[str, Skill]) -> dict[str, OperationSpec]:
    """Every operation an integration declares against one entity, by name."""
    return {
        spec.name: spec
        for skill in by_id.values()
        if skill.type is SkillType.integration
        for spec in skill.frontmatter.operations
        if spec.entity == entity_id
    }


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


def _guardrail_issues(skill: Skill, declared: frozenset[str]) -> list[ValidationIssue]:
    """Every guardrail a skill declares is one the configured catalog defines."""
    return [
        _issue(
            skill,
            "guardrail_unknown_id",
            f"{name} is not declared by the guardrail catalog",
        )
        for name in skill.frontmatter.guardrails
        if name not in declared
    ]


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
