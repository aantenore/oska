"""Wording and capability lists derived from the catalog."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from oska.catalog.registry import SkillRegistry
from oska.catalog.resolver import CapabilityResolver, join_phrases


def test_processes_are_ordered_by_declared_order_then_id(resolver: CapabilityResolver) -> None:
    ids = [skill.id for skill in resolver.available_processes()]
    assert ids[:2] == ["runbook.service_health", "runbook.log_search"]
    assert "composite.incident_triage" in ids
    assert ids[-1] == "runbook.incident_note"


def test_unpublished_processes_are_left_out(
    catalog_copy: Path,
) -> None:
    path = catalog_copy / "runbook" / "service_health" / "v1" / "skill.md"
    path.write_text(
        path.read_text(encoding="utf-8").replace("status: published", "status: retired"),
        encoding="utf-8",
    )
    resolver = CapabilityResolver(SkillRegistry([catalog_copy]))
    assert "runbook.service_health" not in [skill.id for skill in resolver.available_processes()]


def test_action_phrase_falls_back_when_no_selection_wording_exists(
    resolver: CapabilityResolver,
    registry: SkillRegistry,
    write_skill: Callable[..., Path],
    runbook_frontmatter,
) -> None:
    restart = registry.require("runbook.service_restart")
    assert resolver.action_phrase(restart) == "restart a service"
    assert resolver.action_phrase(restart, on_selection=True) == "restart it"

    bare = SkillRegistry([write_skill(runbook_frontmatter()).parents[3]])
    bare_resolver = CapabilityResolver(bare)
    sample = bare.require("runbook.sample")
    assert bare_resolver.action_phrase(sample, on_selection=True) == "do the sample thing"


def test_entity_actions_exclude_reads_and_composites(resolver: CapabilityResolver) -> None:
    ids = [skill.id for skill in resolver.actions_for_entity("entity.service")]
    assert ids == ["runbook.service_restart", "runbook.deployment_rollback"]
    assert [skill.id for skill in resolver.actions_for_entity("entity.alert")] == [
        "runbook.alert_silence"
    ]
    assert [skill.id for skill in resolver.actions_for_entity("entity.incident")] == [
        "runbook.incident_note"
    ]
    assert resolver.actions_for_entity("entity.absent") == []


def test_entity_actions_are_phrased_on_the_selection(resolver: CapabilityResolver) -> None:
    actions = resolver.actions_for_entity("entity.service")
    assert resolver.phrase_list(actions, on_selection=True) == "restart it and roll it back"


def test_declared_scope_names_every_process(resolver: CapabilityResolver) -> None:
    scope = resolver.declared_scope()
    for skill in resolver.available_processes():
        assert resolver.action_phrase(skill) in scope
    assert scope.endswith(".")


def test_declared_scope_of_an_empty_catalog(tmp_path: Path) -> None:
    resolver = CapabilityResolver(SkillRegistry([tmp_path / "absent"]))
    assert resolver.available_processes() == []
    assert resolver.declared_scope() == "No procedure is available."


def test_phrases_are_joined_as_a_sentence() -> None:
    assert join_phrases([]) == ""
    assert join_phrases(["one"]) == "one"
    assert join_phrases(["one", "two"]) == "one and two"
    assert join_phrases(["one", "two", "three"]) == "one, two and three"
    assert join_phrases(["uno", "due"], "e") == "uno e due"


def test_entity_summary_names_the_identifier_states_and_relations(
    resolver: CapabilityResolver,
) -> None:
    summary = resolver.entity_summary("entity.service")
    assert summary.startswith("A service is identified by name;")
    assert "its status is running, degraded or stopped" in summary
    assert "deployments (many deployment)" in summary
    assert summary.endswith(".")

    alert = resolver.entity_summary("entity.alert")
    assert alert.startswith("An alert is identified by id;")
    assert "service (one service)" in alert


def test_entity_summary_of_an_unknown_entity_is_empty(resolver: CapabilityResolver) -> None:
    assert resolver.entity_summary("entity.absent") == ""


def test_entity_summary_leaves_out_what_is_not_declared(
    write_skill: Callable[..., Path],
) -> None:
    frontmatter = {
        "id": "entity.bare",
        "type": "entity",
        "version": 1,
        "description": "An entity with no state and no relations, described at a workable length.",
        "fields": [{"name": "code", "type": "string", "identifier": True}],
    }
    resolver = CapabilityResolver(SkillRegistry([write_skill(frontmatter).parents[3]]))
    assert resolver.entity_summary("entity.bare") == "A bare is identified by code."
