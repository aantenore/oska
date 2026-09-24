"""Loading, validating and reloading the catalog."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path

import pytest

from oska.catalog.loader import SkillParseError, discover, load_skill, parse_sections
from oska.catalog.registry import SkillRegistry
from oska.catalog.validator import validate, validate_all
from oska.contracts.skill import SkillType

EXAMPLE_SKILL_COUNT = 15


def test_discover_finds_every_example(skills_root: Path) -> None:
    paths = discover(skills_root)
    assert len(paths) == EXAMPLE_SKILL_COUNT
    assert all(path.name == "skill.md" for path in paths)


def test_discover_tolerates_a_missing_root(tmp_path: Path) -> None:
    assert discover(tmp_path / "absent") == []


def test_load_skill_parses_frontmatter_and_sections(skills_root: Path) -> None:
    path = skills_root / "runbook" / "service_restart" / "v1" / "skill.md"
    skill = load_skill(path)
    assert skill.id == "runbook.service_restart"
    assert skill.type is SkillType.runbook
    assert skill.is_published
    assert len(skill.frontmatter.triggers) >= 3
    assert skill.frontmatter.runtime is not None
    assert skill.frontmatter.runtime.action_on_selection == "restart it"
    assert skill.frontmatter.capabilities.writes == ["service.restart"]
    assert skill.frontmatter.policy == "policy.json"
    assert "service.restart" in skill.sections.steps
    assert skill.sections.rollback.startswith("A restart cannot be undone")
    assert skill.fingerprint == hashlib.sha256(path.read_bytes()).hexdigest()


def test_integration_declares_verification_for_every_write(skills_root: Path) -> None:
    skill = load_skill(skills_root / "integration" / "container_runtime" / "v1" / "skill.md")
    restart = skill.frontmatter.operation("service.restart")
    assert restart is not None
    assert restart.write is True
    assert restart.verify is not None
    assert restart.verify.operation == "service.status"
    assert restart.verify.expect["service.name"] == "$payload.service"
    assert restart.idempotency_fields == ["service"]


def test_unknown_sections_are_kept(write_skill: Callable[..., Path], runbook_frontmatter) -> None:
    body = "## Purpose\n\nWhy.\n\n## Escalation\n\nPage the owning team.\n"
    skill = load_skill(write_skill(runbook_frontmatter(), body))
    assert skill.sections.purpose == "Why."
    assert skill.sections.extra == {"escalation": "Page the owning team."}


def test_body_without_headings_is_empty() -> None:
    sections = parse_sections("no headings here")
    assert sections.purpose == ""
    assert sections.extra == {}


def test_frontmatter_must_be_present(tmp_path: Path) -> None:
    path = tmp_path / "skill.md"
    path.write_text("## Purpose\n\nNothing above.\n", encoding="utf-8")
    with pytest.raises(SkillParseError):
        load_skill(path)


def test_a_write_operation_needs_a_read_back(write_skill: Callable[..., Path]) -> None:
    frontmatter = {
        "id": "integration.sample",
        "type": "integration",
        "version": 1,
        "description": "An integration used by the tests, with a write that forgets the read-back.",
        "operations": [
            {
                "name": "thing.change",
                "write": True,
                "input_schema": {"type": "object"},
                "output_schema": {"type": "object"},
            }
        ],
    }
    with pytest.raises(SkillParseError, match="verify"):
        load_skill(write_skill(frontmatter))


def test_examples_validate_without_issues(registry: SkillRegistry, schemas_dir: Path) -> None:
    assert registry.parse_errors() == []
    issues = validate_all(registry.skills(), schema_dir=schemas_dir)
    assert [str(issue) for issue in issues] == []


def test_unknown_reference_is_reported(
    write_skill: Callable[..., Path],
    runbook_frontmatter,
    schemas_dir: Path,
) -> None:
    skill = load_skill(write_skill(runbook_frontmatter(entities=["entity.absent"])))
    codes = {issue.code for issue in validate(skill, [skill], schema_dir=schemas_dir)}
    assert codes == {"unknown_reference"}


def test_a_capability_must_be_declared_by_an_integration(
    registry: SkillRegistry,
    write_skill: Callable[..., Path],
    runbook_frontmatter,
    schemas_dir: Path,
) -> None:
    frontmatter = runbook_frontmatter(
        integrations=["integration.container_runtime"],
        capabilities={"reads": ["service.status"], "writes": ["service.invented"]},
        policy="policy.json",
    )
    policy = {
        "skill_id": "runbook.sample",
        "default": "deny",
        "rules": [{"operation": "service.invented", "effect": "allow"}],
    }
    skill = load_skill(write_skill(frontmatter, policy=policy))
    issues = validate(skill, [skill, *registry.skills()], schema_dir=schemas_dir)
    assert [issue.code for issue in issues] == ["undeclared_capability"]


def test_a_procedure_that_writes_needs_a_policy(
    registry: SkillRegistry,
    write_skill: Callable[..., Path],
    runbook_frontmatter,
    schemas_dir: Path,
) -> None:
    frontmatter = runbook_frontmatter(
        integrations=["integration.container_runtime"],
        capabilities={"writes": ["service.restart"]},
    )
    skill = load_skill(write_skill(frontmatter))
    issues = validate(skill, [skill, *registry.skills()], schema_dir=schemas_dir)
    assert [issue.code for issue in issues] == ["missing_policy"]


def test_a_composite_carries_no_policy_of_its_own(
    registry: SkillRegistry, schemas_dir: Path
) -> None:
    composite = registry.require("composite.incident_triage")
    assert composite.frontmatter.policy is None
    assert composite.frontmatter.capabilities.writes == ["incident.note"]
    assert set(composite.frontmatter.capabilities.reads) == {
        "service.status",
        "service.logs",
        "incident.get",
    }
    assert validate(composite, registry.skills(), schema_dir=schemas_dir) == []


def test_closure_reaches_everything_a_composite_uses(registry: SkillRegistry) -> None:
    hydrated = registry.closure("composite.incident_triage")
    assert hydrated.runbook.id == "composite.incident_triage"
    assert [skill.id for skill in hydrated.runbooks] == [
        "runbook.service_health",
        "runbook.log_search",
        "runbook.incident_note",
    ]
    assert [skill.id for skill in hydrated.integrations] == [
        "integration.container_runtime",
        "integration.incident_tracker",
    ]
    assert "entity.service" in [skill.id for skill in hydrated.entities]
    assert ("integration.container_runtime", "service.restart") in hydrated.operations()


def test_closure_of_a_runbook_holds_no_runbooks(registry: SkillRegistry) -> None:
    hydrated = registry.closure("runbook.service_health")
    assert hydrated.runbooks == []
    assert [skill.id for skill in hydrated.integrations] == ["integration.container_runtime"]


def test_registry_reloads_when_a_file_changes(catalog_copy: Path) -> None:
    registry = SkillRegistry([catalog_copy])
    before = registry.fingerprint()
    assert registry.get("runbook.service_health") is not None
    assert registry.get("runbook.invented") is None

    path = catalog_copy / "runbook" / "service_health" / "v1" / "skill.md"
    path.write_text(
        path.read_text(encoding="utf-8").replace("order: 1", "order: 9"), encoding="utf-8"
    )

    assert registry.fingerprint() != before
    reloaded = registry.require("runbook.service_health")
    assert reloaded.frontmatter.runtime is not None
    assert reloaded.frontmatter.runtime.order == 9


def test_registry_prefers_the_highest_published_version(catalog_copy: Path) -> None:
    source = catalog_copy / "runbook" / "service_health" / "v1" / "skill.md"
    text = source.read_text(encoding="utf-8")

    second = catalog_copy / "runbook" / "service_health" / "v2"
    second.mkdir()
    (second / "skill.md").write_text(text.replace("version: 1", "version: 2"), encoding="utf-8")

    third = catalog_copy / "runbook" / "service_health" / "v3"
    third.mkdir()
    (third / "skill.md").write_text(
        text.replace("version: 1", "version: 3").replace("status: published", "status: draft"),
        encoding="utf-8",
    )

    registry = SkillRegistry([catalog_copy])
    assert len([skill for skill in registry.skills() if skill.id == "runbook.service_health"]) == 3
    assert registry.require("runbook.service_health").version == 2
    assert all(skill.is_published for skill in registry.published())


def test_a_file_that_cannot_be_parsed_is_reported(catalog_copy: Path) -> None:
    broken = catalog_copy / "runbook" / "broken" / "v1"
    broken.mkdir(parents=True)
    (broken / "skill.md").write_text("not a skill at all\n", encoding="utf-8")
    registry = SkillRegistry([catalog_copy])
    errors = registry.parse_errors()
    assert len(errors) == 1
    assert errors[0].code == "parse_error"
    assert len(registry.skills()) == EXAMPLE_SKILL_COUNT


def test_every_policy_names_its_own_procedure(registry: SkillRegistry) -> None:
    for skill in registry.published():
        relative = skill.frontmatter.policy
        if relative is None:
            continue
        payload = json.loads((skill.path.parent / relative).read_text(encoding="utf-8"))
        assert payload["skill_id"] == skill.id
        assert payload["default"] == "deny"


def test_registry_groups_by_type(registry: SkillRegistry) -> None:
    assert len(registry.by_type(SkillType.entity)) == 4
    assert len(registry.by_type(SkillType.integration)) == 4
    assert len(registry.by_type("runbook")) == 6
    assert len(registry.by_type(SkillType.composite)) == 1


def _entity(**overrides: object) -> dict:
    frontmatter: dict = {
        "id": "entity.thing",
        "type": "entity",
        "version": 1,
        "description": "A thing the tests declare, described at length enough to pass the checks.",
        "fields": [
            {"name": "id", "type": "string", "identifier": True},
            {"name": "state", "type": "string"},
        ],
    }
    frontmatter.update(overrides)
    return frontmatter


def _integration(**overrides: object) -> dict:
    frontmatter: dict = {
        "id": "integration.thing_control",
        "type": "integration",
        "version": 1,
        "description": "An integration the tests declare, described at length enough to pass.",
        "entities": ["entity.thing"],
        "operations": [
            {
                "name": "thing.get",
                "write": False,
                "entity": "entity.thing",
                "input_schema": {"type": "object"},
                "output_schema": {"type": "object"},
            },
            {
                "name": "thing.change",
                "write": True,
                "entity": "entity.thing",
                "input_schema": {"type": "object"},
                "output_schema": {"type": "object"},
                "verify": {"operation": "thing.get"},
            },
        ],
    }
    frontmatter.update(overrides)
    return frontmatter


def _codes(
    frontmatter: dict,
    others: list[dict],
    write_skill: Callable[..., Path],
    schemas_dir: Path,
) -> set[str]:
    written = [load_skill(write_skill(other)) for other in others]
    skill = load_skill(write_skill(frontmatter))
    return {issue.code for issue in validate(skill, [skill, *written], schema_dir=schemas_dir)}


def test_a_state_field_must_name_a_declared_field(
    write_skill: Callable[..., Path], schemas_dir: Path
) -> None:
    frontmatter = _entity(state_field="absent", states=["open"])
    assert _codes(frontmatter, [], write_skill, schemas_dir) == {"ontology_unknown_state_field"}


def test_a_state_field_needs_allowed_values(
    write_skill: Callable[..., Path], schemas_dir: Path
) -> None:
    assert _codes(_entity(state_field="state"), [], write_skill, schemas_dir) == {
        "ontology_missing_states"
    }
    assert _codes(_entity(states=["open"]), [], write_skill, schemas_dir) == {
        "ontology_missing_states"
    }


def test_a_relation_must_target_a_published_entity(
    write_skill: Callable[..., Path], schemas_dir: Path
) -> None:
    relation = {"name": "others", "target": "entity.absent", "cardinality": "many"}
    assert _codes(_entity(relations=[relation]), [], write_skill, schemas_dir) == {
        "ontology_unknown_relation_target"
    }

    other = _entity(id="entity.other", status="draft")
    relation = {"name": "others", "target": "entity.other", "cardinality": "one"}
    assert _codes(_entity(relations=[relation]), [other], write_skill, schemas_dir) == {
        "ontology_unpublished_relation_target"
    }


def test_an_operation_acts_on_an_entity_the_integration_lists(
    write_skill: Callable[..., Path], schemas_dir: Path
) -> None:
    other = _entity(id="entity.other")
    frontmatter = _integration()
    frontmatter["operations"][0]["entity"] = "entity.other"
    codes = _codes(frontmatter, [_entity(), other], write_skill, schemas_dir)
    assert codes == {"ontology_unlisted_operation_entity"}


def test_a_transition_needs_an_entity_to_leave_in_a_state(
    write_skill: Callable[..., Path], schemas_dir: Path
) -> None:
    frontmatter = _integration()
    frontmatter["operations"][1]["entity"] = None
    frontmatter["operations"][1]["transitions"] = {"state": "closed"}
    codes = _codes(frontmatter, [_entity()], write_skill, schemas_dir)
    assert codes == {"ontology_transition_without_entity", "ontology_unbound_operation"}


def test_an_operation_must_be_bound_to_an_entity(
    write_skill: Callable[..., Path], schemas_dir: Path
) -> None:
    frontmatter = _integration()
    frontmatter["operations"][0].pop("entity")
    codes = _codes(frontmatter, [_entity()], write_skill, schemas_dir)
    assert codes == {"ontology_unbound_operation"}


def test_an_unpublished_integration_may_still_be_unbound(
    write_skill: Callable[..., Path], schemas_dir: Path
) -> None:
    frontmatter = _integration(status="draft")
    frontmatter["operations"][0].pop("entity")
    assert _codes(frontmatter, [_entity()], write_skill, schemas_dir) == set()


def test_a_transition_is_keyed_by_the_state_field_of_its_entity(
    write_skill: Callable[..., Path], schemas_dir: Path
) -> None:
    entity = _entity(state_field="state", states=["open", "closed"])
    frontmatter = _integration()
    frontmatter["operations"][1]["transitions"] = {"id": "closed"}
    codes = _codes(frontmatter, [entity], write_skill, schemas_dir)
    assert codes == {"ontology_unknown_transition_field"}


def test_a_transition_names_a_declared_state(
    write_skill: Callable[..., Path], schemas_dir: Path
) -> None:
    entity = _entity(state_field="state", states=["open", "closed"])
    frontmatter = _integration()
    frontmatter["operations"][1]["transitions"] = {"state": "invented"}
    codes = _codes(frontmatter, [entity], write_skill, schemas_dir)
    assert codes == {"ontology_unknown_transition_state"}

    frontmatter["operations"][1]["transitions"] = {"state": "closed"}
    assert _codes(frontmatter, [entity], write_skill, schemas_dir) == set()
