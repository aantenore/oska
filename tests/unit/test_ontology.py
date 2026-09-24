"""The graph derived from the declared entities and what binds to them."""

from __future__ import annotations

from pathlib import Path

from oska.catalog.registry import SkillRegistry
from oska.ontology.export import from_json, to_json, to_mermaid
from oska.ontology.graph import OntologyCache, OntologyGraph, operation_node_id
from oska.ontology.queries import (
    operations_on,
    path,
    procedures_for,
    related,
    vocabulary,
    writers_of,
)


def test_every_published_declaration_becomes_a_node(registry: SkillRegistry) -> None:
    graph = OntologyGraph.from_registry(registry)
    assert [node.id for node in graph.nodes_of("entity")] == [
        "entity.alert",
        "entity.deployment",
        "entity.incident",
        "entity.service",
    ]
    assert len(graph.nodes_of("integration")) == 4
    assert len(graph.nodes_of("procedure")) == 6
    assert [node.id for node in graph.nodes_of("composite")] == ["composite.incident_triage"]
    assert len(graph.nodes_of("operation")) == 12
    assert {node.id for node in graph.nodes_of("state")} == {
        "entity.alert#firing",
        "entity.alert#silenced",
        "entity.alert#resolved",
        "entity.deployment#in_progress",
        "entity.deployment#succeeded",
        "entity.deployment#failed",
        "entity.deployment#rolled_back",
    }
    assert graph.fingerprint == registry.fingerprint()


def test_an_entity_node_carries_its_identifier_states_and_fields(registry: SkillRegistry) -> None:
    node = OntologyGraph.from_registry(registry).node("entity.service")
    assert node is not None
    assert node.attributes["identifier"] == "name"
    assert node.attributes["state_field"] == "status"
    assert node.attributes["states"] == ["running", "degraded", "stopped"]
    assert [field["name"] for field in node.attributes["fields"]][:2] == ["name", "environment"]


def test_relations_are_edges_with_a_name_and_a_cardinality(registry: SkillRegistry) -> None:
    graph = OntologyGraph.from_registry(registry)
    edges = graph.edges_from("entity.service", "relates_to")
    assert {edge.target for edge in edges} == {
        "entity.alert",
        "entity.deployment",
        "entity.incident",
    }
    assert all(edge.attributes["cardinality"] == "many" for edge in edges)
    back = graph.edges_from("entity.alert", "relates_to")
    assert [(edge.target, edge.attributes["cardinality"]) for edge in back] == [
        ("entity.service", "one")
    ]


def test_an_operation_is_declared_by_one_integration_and_acts_on_one_entity(
    registry: SkillRegistry,
) -> None:
    graph = OntologyGraph.from_registry(registry)
    node_id = operation_node_id("integration.container_runtime", "service.restart")
    node = graph.node(node_id)
    assert node is not None
    assert node.attributes["write"] is True
    assert node.attributes["transitions"] == {"status": "running"}
    assert [edge.source for edge in graph.edges_to(node_id, "declares")] == [
        "integration.container_runtime"
    ]
    assert [edge.target for edge in graph.edges_from(node_id, "acts_on")] == ["entity.service"]


def test_a_process_uses_integrations_touches_entities_and_contains_members(
    registry: SkillRegistry,
) -> None:
    graph = OntologyGraph.from_registry(registry)
    assert [edge.target for edge in graph.edges_from("runbook.deployment_rollback", "uses")] == [
        "integration.container_runtime",
        "integration.deployment_control",
    ]
    assert [edge.target for edge in graph.edges_from("runbook.deployment_rollback", "touches")] == [
        "entity.deployment",
        "entity.service",
    ]
    assert [edge.target for edge in graph.edges_from("composite.incident_triage", "contains")] == [
        "runbook.incident_note",
        "runbook.log_search",
        "runbook.service_health",
    ]


def test_an_unpublished_skill_is_left_out(catalog_copy: Path) -> None:
    target = catalog_copy / "entity" / "alert" / "v1" / "skill.md"
    target.write_text(
        target.read_text(encoding="utf-8").replace("status: published", "status: retired"),
        encoding="utf-8",
    )
    graph = OntologyGraph.from_registry(SkillRegistry([catalog_copy]))
    assert graph.node("entity.alert") is None
    assert graph.edges_to("entity.alert") == []
    assert graph.edges_from("entity.service", "relates_to") != []


def test_operations_on_an_entity_can_be_narrowed_to_writes(registry: SkillRegistry) -> None:
    graph = OntologyGraph.from_registry(registry)
    assert [node.label for node in operations_on(graph, "entity.service")] == [
        "service.logs",
        "service.restart",
        "service.scale",
        "service.status",
    ]
    assert [node.label for node in operations_on(graph, "entity.service", writes_only=True)] == [
        "service.restart",
        "service.scale",
    ]
    assert operations_on(graph, "entity.absent") == []


def test_writers_of_an_entity_are_the_integrations_that_change_it(
    registry: SkillRegistry,
) -> None:
    graph = OntologyGraph.from_registry(registry)
    assert [node.id for node in writers_of(graph, "entity.deployment")] == [
        "integration.deployment_control"
    ]
    assert [node.id for node in writers_of(graph, "entity.alert")] == ["integration.alerting"]
    assert writers_of(graph, "entity.absent") == []


def test_procedures_for_an_entity_exclude_composites(registry: SkillRegistry) -> None:
    graph = OntologyGraph.from_registry(registry)
    assert [node.id for node in procedures_for(graph, "entity.service")] == [
        "runbook.service_health",
        "runbook.log_search",
        "runbook.service_restart",
        "runbook.deployment_rollback",
    ]
    assert [
        node.id for node in procedures_for(graph, "entity.service", writes_only=True)
    ] == ["runbook.service_restart", "runbook.deployment_rollback"]


def test_related_reports_the_name_target_and_cardinality(registry: SkillRegistry) -> None:
    graph = OntologyGraph.from_registry(registry)
    assert related(graph, "entity.service") == [
        ("alerts", "entity.alert", "many"),
        ("deployments", "entity.deployment", "many"),
        ("incidents", "entity.incident", "many"),
    ]
    assert related(graph, "entity.incident") == [("service", "entity.service", "one")]
    assert related(graph, "entity.absent") == []


def test_a_path_runs_over_relations_only(registry: SkillRegistry) -> None:
    graph = OntologyGraph.from_registry(registry)
    assert path(graph, "entity.service", "entity.alert") == ["entity.service", "entity.alert"]
    assert path(graph, "entity.alert", "entity.deployment") == [
        "entity.alert",
        "entity.service",
        "entity.deployment",
    ]
    assert path(graph, "entity.service", "entity.service") == ["entity.service"]
    assert path(graph, "entity.service", "entity.absent") is None
    assert path(graph, "entity.service", "integration.alerting") is None


def test_vocabulary_names_every_entity(registry: SkillRegistry) -> None:
    entries = vocabulary(OntologyGraph.from_registry(registry))
    assert set(entries) == {
        "entity.alert",
        "entity.deployment",
        "entity.incident",
        "entity.service",
    }
    assert entries["entity.alert"] == {
        "description": entries["entity.alert"]["description"],
        "identifier": "id",
        "state_field": "state",
        "states": ["firing", "silenced", "resolved"],
        "fields": ["id", "service", "rule", "state", "silenced_until"],
    }


def test_json_survives_a_round_trip(registry: SkillRegistry) -> None:
    graph = OntologyGraph.from_registry(registry)
    payload = to_json(graph)
    assert set(payload) == {"nodes", "edges", "fingerprint"}
    assert from_json(payload) == graph


def test_mermaid_holds_a_subgraph_per_kind_and_the_edges(registry: SkillRegistry) -> None:
    graph = OntologyGraph.from_registry(registry)
    diagram = to_mermaid(graph)
    assert diagram.startswith("graph LR")
    for title in ("Entities", "Integrations", "Operations", "Procedures", "Composites"):
        assert f"subgraph {title}" in diagram
    assert "entity_service -->|deployments (many)| entity_deployment" in diagram
    assert (
        "integration_container_runtime_service_restart -->|acts on (write)| entity_service"
        in diagram
    )
    assert "runbook_service_restart -->|uses| integration_container_runtime" in diagram


def test_mermaid_can_leave_the_operations_out(registry: SkillRegistry) -> None:
    diagram = to_mermaid(OntologyGraph.from_registry(registry), include_operations=False)
    assert "subgraph Operations" not in diagram
    assert "service.restart" not in diagram
    assert "acts on" not in diagram
    assert "subgraph Entities" in diagram


def test_the_cache_rebuilds_when_the_catalog_changes(catalog_copy: Path) -> None:
    registry = SkillRegistry([catalog_copy])
    cache = OntologyCache(registry)
    first = cache.graph()
    assert cache.graph() is first

    target = catalog_copy / "entity" / "service" / "v1" / "skill.md"
    target.write_text(
        target.read_text(encoding="utf-8").replace("  - stopped\n", "  - stopped\n  - draining\n"),
        encoding="utf-8",
    )

    second = cache.graph()
    assert second is not first
    assert second.fingerprint != first.fingerprint
    node = second.node("entity.service")
    assert node is not None
    assert "draining" in node.attributes["states"]
