"""Ontology and lifecycle checks: states, relations, moves and the operations bound to an entity."""

from __future__ import annotations

from oska.catalog.checks.common import ValidationIssue, issue
from oska.contracts.skill import OperationSpec, Skill, SkillType

__all__ = ["ontology_issues"]


def ontology_issues(skill: Skill, by_id: dict[str, Skill]) -> list[ValidationIssue]:
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
                issue(
                    skill,
                    "ontology_unknown_state_field",
                    f"{frontmatter.state_field} is not a declared field",
                )
            )
        if not frontmatter.states:
            issues.append(
                issue(
                    skill,
                    "ontology_missing_states",
                    f"{frontmatter.state_field} declares no allowed values",
                )
            )
    elif frontmatter.states:
        issues.append(
            issue(skill, "ontology_missing_states", "states are declared without a state field")
        )
    issues.extend(_lifecycle_issues(skill, by_id))
    for relation in frontmatter.relations:
        target = by_id.get(relation.target)
        if target is None or target.type is not SkillType.entity:
            issues.append(
                issue(
                    skill,
                    "ontology_unknown_relation_target",
                    f"relation {relation.name}: {relation.target} is not an entity in the catalog",
                )
            )
        elif not target.is_published:
            issues.append(
                issue(
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
            issue(
                skill,
                "ontology_lifecycle_without_states",
                "a lifecycle needs a state field and the states it may hold",
            )
        )
    issues.extend(_named_state_issues(skill, states))
    if frontmatter.initial_state is None:
        issues.append(
            issue(
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
        issue(
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
                issue(
                    skill,
                    "ontology_unknown_lifecycle_operation",
                    f"{move.operation} is not an operation bound to {skill.id}",
                )
            )
        elif not spec.write:
            issues.append(
                issue(
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
            issue(
                skill,
                "ontology_missing_state_read",
                "a lifecycle names the read the current state is taken from",
            )
        ]
    spec = _bound_operations(skill.id, by_id).get(frontmatter.state_read)
    if spec is None:
        return [
            issue(
                skill,
                "ontology_unknown_state_read",
                f"{frontmatter.state_read} is not an operation bound to {skill.id}",
            )
        ]
    if spec.write:
        return [
            issue(
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
        issue(
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
        issue(
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
        issue(
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
                    issue(
                        skill,
                        "ontology_unbound_operation",
                        f"{spec.name}: an operation is bound to exactly one entity type",
                    )
                )
        elif spec.entity not in listed:
            issues.append(
                issue(
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
                issue(
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
                    issue(
                        skill,
                        "ontology_unknown_transition_field",
                        f"{spec.name}: {key} is not the state field of {entity.id}",
                    )
                )
                continue
            if value not in entity.frontmatter.states:
                issues.append(
                    issue(
                        skill,
                        "ontology_unknown_transition_state",
                        f"{spec.name}: {value} is not a declared state of {entity.id}",
                    )
                )
    return issues
