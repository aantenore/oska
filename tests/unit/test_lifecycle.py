"""The declared lifecycle of an entity: what it must hold, and what it refuses.

The state a move is judged against is never taken from the conversation. Every
test here that asks whether a write may run answers the question the way the
runtime does: from what the entity's declared read reports.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from opsdeck.audit.sink import MemorySink
from opsdeck.catalog.loader import load_skill
from opsdeck.catalog.registry import SkillRegistry
from opsdeck.catalog.resolver import CapabilityResolver
from opsdeck.catalog.validator import validate, validate_all
from opsdeck.config import Settings
from opsdeck.contracts.common import ConnectorResult, Hydrated
from opsdeck.contracts.plan import Plan
from opsdeck.contracts.receipt import VerificationStatus
from opsdeck.engine.engine import Engine
from opsdeck.engine.factory import build_engine
from opsdeck.governance.capabilities import CapabilityRegistry
from opsdeck.governance.verification import Verifier, read_state
from opsdeck.ontology.export import from_json, to_json, to_mermaid
from opsdeck.ontology.graph import OntologyGraph, state_node_id
from opsdeck.ontology.queries import legal_moves, lifecycle_of, required_move
from opsdeck.planner.planner import validate_plan
from opsdeck.planner.scripted import ScriptedModelProvider

ALERTING = "integration.alerting"
SILENCED_ALERT = "alt-9003"


class ReadBack:
    """A connector that answers every read with the record the test gave it."""

    def __init__(self, data: dict) -> None:
        self._data = data

    def execute(self, operation: str, payload: dict) -> ConnectorResult:
        """Answer with the record, whatever was asked for."""
        return ConnectorResult(ok=True, data=self._data)


@pytest.fixture
def access_registry(repo_root: Path) -> SkillRegistry:
    """Registry over the access example catalog."""
    return SkillRegistry([repo_root / "examples" / "access" / "skills"])


@pytest.fixture
def access_resolver(access_registry: SkillRegistry) -> CapabilityResolver:
    """Resolver over the access example catalog."""
    return CapabilityResolver(access_registry)


# --- what a declared lifecycle has to hold -----------------------------------


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
        "state_field": "state",
        "states": ["open", "settled", "withdrawn"],
        "initial_state": "open",
        "state_read": "thing.get",
        "lifecycle": [{"from": ["open"], "to": "settled", "operation": "thing.change"}],
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
    write_skill: Callable[..., Path],
    schemas_dir: Path,
) -> set[str]:
    integration = load_skill(write_skill(_integration()))
    skill = load_skill(write_skill(frontmatter))
    return {
        issue.code for issue in validate(skill, [skill, integration], schema_dir=schemas_dir)
    }


def test_a_well_formed_lifecycle_raises_nothing(
    write_skill: Callable[..., Path], schemas_dir: Path
) -> None:
    assert _codes(_entity(), write_skill, schemas_dir) == set()


def test_every_state_a_move_names_is_one_the_entity_declares(
    write_skill: Callable[..., Path], schemas_dir: Path
) -> None:
    unknown_target = _entity(
        lifecycle=[{"from": ["open"], "to": "absent", "operation": "thing.change"}]
    )
    assert "ontology_unknown_lifecycle_state" in _codes(unknown_target, write_skill, schemas_dir)
    unknown_source = _entity(
        lifecycle=[{"from": ["absent"], "to": "settled", "operation": "thing.change"}]
    )
    assert "ontology_unknown_lifecycle_state" in _codes(unknown_source, write_skill, schemas_dir)
    assert "ontology_unknown_lifecycle_state" in _codes(
        _entity(initial_state="absent"), write_skill, schemas_dir
    )


def test_a_lifecycle_needs_a_state_field_its_states_and_a_starting_point(
    write_skill: Callable[..., Path], schemas_dir: Path
) -> None:
    assert "ontology_lifecycle_without_states" in _codes(
        _entity(state_field=None, states=[]), write_skill, schemas_dir
    )
    assert "ontology_lifecycle_without_initial_state" in _codes(
        _entity(initial_state=None), write_skill, schemas_dir
    )


def test_a_move_names_a_write_bound_to_this_entity(
    write_skill: Callable[..., Path], schemas_dir: Path
) -> None:
    absent = _entity(lifecycle=[{"from": ["open"], "to": "settled", "operation": "thing.wish"}])
    assert _codes(absent, write_skill, schemas_dir) == {"ontology_unknown_lifecycle_operation"}
    read = _entity(lifecycle=[{"from": ["open"], "to": "settled", "operation": "thing.get"}])
    assert _codes(read, write_skill, schemas_dir) == {"ontology_unknown_lifecycle_operation"}


def test_the_state_is_read_back_through_a_declared_read(
    write_skill: Callable[..., Path], schemas_dir: Path
) -> None:
    assert _codes(_entity(state_read=None), write_skill, schemas_dir) == {
        "ontology_missing_state_read"
    }
    assert _codes(_entity(state_read="thing.look"), write_skill, schemas_dir) == {
        "ontology_unknown_state_read"
    }
    assert _codes(_entity(state_read="thing.change"), write_skill, schemas_dir) == {
        "ontology_unknown_state_read"
    }


def test_nothing_moves_out_of_a_final_state(
    write_skill: Callable[..., Path], schemas_dir: Path
) -> None:
    frontmatter = _entity(
        final_states=["open"],
        lifecycle=[{"from": ["open"], "to": "settled", "operation": "thing.change"}],
    )
    assert _codes(frontmatter, write_skill, schemas_dir) == {"ontology_move_from_final_state"}


def test_a_state_nothing_can_lead_to_is_reported(
    write_skill: Callable[..., Path], schemas_dir: Path
) -> None:
    frontmatter = _entity(
        lifecycle=[
            {"from": ["open"], "to": "settled", "operation": "thing.change"},
            {"from": ["absent_island"], "to": "withdrawn", "operation": "thing.change"},
        ],
        states=["open", "settled", "withdrawn", "absent_island"],
    )
    assert _codes(frontmatter, write_skill, schemas_dir) == {"ontology_unreachable_state"}


def test_one_state_declares_at_most_one_required_move(
    write_skill: Callable[..., Path], schemas_dir: Path
) -> None:
    frontmatter = _entity(
        lifecycle=[
            {"from": ["open"], "to": "settled", "operation": "thing.change", "required": True},
            {"from": ["open"], "to": "withdrawn", "operation": "thing.change", "required": True},
        ]
    )
    assert _codes(frontmatter, write_skill, schemas_dir) == {
        "ontology_conflicting_required_move"
    }


def test_both_example_catalogs_stay_valid(repo_root: Path, schemas_dir: Path) -> None:
    for domain in ("ops", "access"):
        skills = SkillRegistry([repo_root / "examples" / domain / "skills"]).skills()
        assert validate_all(skills, schema_dir=schemas_dir) == []


# --- what the graph holds ----------------------------------------------------


def test_a_lifecycle_becomes_states_and_moves_in_the_graph(registry: SkillRegistry) -> None:
    graph = OntologyGraph.from_registry(registry)
    node = graph.node(state_node_id("entity.deployment", "rolled_back"))
    assert node is not None
    assert node.kind == "state"
    assert node.attributes["final"] is True
    assert node.attributes["entity"] == "entity.deployment"
    origin = state_node_id("entity.deployment", "succeeded")
    assert [edge.target for edge in graph.edges_from(origin, "moves_to")] == [
        state_node_id("entity.deployment", "rolled_back")
    ]


def test_the_lifecycle_of_an_entity_is_read_from_the_graph(registry: SkillRegistry) -> None:
    graph = OntologyGraph.from_registry(registry)
    lifecycle = lifecycle_of(graph, "entity.deployment")
    assert lifecycle is not None
    assert lifecycle.initial == "in_progress"
    assert lifecycle.final_states == ["rolled_back"]
    assert lifecycle.state_read == "deployment.get"
    assert lifecycle.is_final("rolled_back")
    assert {move.from_state for move in lifecycle.moves} == {
        "in_progress",
        "succeeded",
        "failed",
    }
    assert lifecycle_of(graph, "entity.incident") is None
    assert lifecycle_of(graph, "entity.absent") is None


def test_legal_moves_and_the_required_one(access_registry: SkillRegistry) -> None:
    graph = OntologyGraph.from_registry(access_registry)
    moves = legal_moves(graph, "entity.access_request", "open")
    assert [(move.to, move.operation, move.required) for move in moves] == [
        ("granted", "request.close", True)
    ]
    required = required_move(graph, "entity.access_request", "open")
    assert required is not None
    assert required.to == "granted"
    held = legal_moves(graph, "entity.access_request", "granted")
    assert [(move.to, move.offered) for move in held] == [("revoked", False)]
    assert required_move(graph, "entity.access_request", "granted") is None
    assert legal_moves(graph, "entity.access_request", "revoked") == []


def test_the_graph_still_survives_a_round_trip(registry: SkillRegistry) -> None:
    graph = OntologyGraph.from_registry(registry)
    assert from_json(to_json(graph)) == graph


def test_mermaid_holds_a_state_diagram_per_lifecycle(registry: SkillRegistry) -> None:
    diagram = to_mermaid(OntologyGraph.from_registry(registry))
    assert diagram.startswith("graph LR")
    assert "%% lifecycle of entity.deployment" in diagram
    assert "stateDiagram-v2" in diagram
    assert "  [*] --> in_progress" in diagram
    assert "  succeeded --> rolled_back: deployment.rollback" in diagram
    assert "  rolled_back --> [*]" in diagram
    assert "entity_deployment_rolled_back" not in diagram


# --- what the runtime refuses ------------------------------------------------


def _engine(
    repo_root: Path,
    state_dir: Path,
    domain: str,
    script: list[dict],
    connectors: Path | None = None,
) -> Engine:
    root = repo_root / "examples" / domain
    settings = Settings(
        skills_roots=[root / "skills"],
        runtime_config=repo_root / "configs" / "runtime.json",
        model_config_path=repo_root / "configs" / "model.json",
        connectors_config=connectors or root / "configs" / "connectors.json",
        schemas_dir=repo_root / "schemas",
        state_dir=state_dir,
        provider="scripted",
    )
    return build_engine(
        settings, provider=ScriptedModelProvider(script), audit_sink=MemorySink()
    )


def test_a_move_out_of_a_state_that_has_none_is_refused_before_the_write(
    repo_root: Path, tmp_path: Path
) -> None:
    script = [
        {
            "response": {
                "kind": "new_task",
                "runbook_id": "runbook.alert_silence",
                "confidence": 0.9,
                "language": "en",
            }
        },
        {
            "response": {
                "runbook_id": "runbook.alert_silence",
                "operations": [
                    {
                        "integration_id": ALERTING,
                        "operation": "alert.silence",
                        "payload": {"alert_id": SILENCED_ALERT, "minutes": 30},
                        "write": True,
                        "purpose": "quieten the alert",
                    }
                ],
                "missing": [],
                "reply_intent": "report the silence",
            }
        },
        {"response": {"reply": "Nothing was silenced."}},
    ]
    engine = _engine(repo_root, tmp_path / "state", "ops", script)
    result = engine.turn("session", f"silence {SILENCED_ALERT} for half an hour")
    assert [outcome.kind for outcome in result.outcomes] == ["invalid_plan"]
    assert result.pending is None
    assert engine.receipts("session") == []
    issues = result.outcomes[0].data["issues"]
    assert "the alert is silenced" in issues[0]
    assert "alert.silence is not a legal move" in issues[0]


DEPLOYMENT_CONTROL = "integration.deployment_control"
CONTAINER_RUNTIME = "integration.container_runtime"


def _ops_connectors(repo_root: Path, tmp_path: Path, payments_state: str) -> Path:
    """The operational bindings, with a deployment fixture this test writes.

    Everything else is the shipped configuration: the same catalog, the same
    operations, the same connector kinds. Only what the deployment records hold
    is the test's, because the question is which object a state was read from.
    """
    fixture = tmp_path / "deployments.json"
    fixture.write_text(
        json.dumps(
            {
                "deployments": [
                    {
                        "id": "dep-2001",
                        "service": "checkout",
                        "version": "2.4.1",
                        "previous_version": "2.4.0",
                        "state": "succeeded",
                        "current": True,
                    },
                    {
                        "id": "dep-2002",
                        "service": "payments",
                        "version": "3.2.0",
                        "previous_version": "3.1.7",
                        "state": payments_state,
                        "current": True,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    shipped = repo_root / "examples" / "ops" / "configs" / "connectors.json"
    document = json.loads(shipped.read_text(encoding="utf-8"))
    for integration_id, spec in document["integrations"].items():
        spec["fixture"] = str((shipped.parent / spec["fixture"]).resolve())
        if integration_id == DEPLOYMENT_CONTROL:
            spec["fixture"] = str(fixture)
    path = tmp_path / "connectors.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _rollback_plan(read_for: str, rolled_back: str) -> dict:
    """A plan that reads the deployment of one service and rolls back another's."""
    return {
        "response": {
            "runbook_id": "runbook.deployment_rollback",
            "operations": [
                {
                    "integration_id": CONTAINER_RUNTIME,
                    "operation": "service.status",
                    "payload": {"service": rolled_back},
                    "write": False,
                    "purpose": "read the environment the service runs in",
                },
                {
                    "integration_id": DEPLOYMENT_CONTROL,
                    "operation": "deployment.get",
                    "payload": {"service": read_for},
                    "write": False,
                    "purpose": "read a deployment",
                },
                {
                    "integration_id": DEPLOYMENT_CONTROL,
                    "operation": "deployment.rollback",
                    "payload": {"service": rolled_back, "target_version": "3.1.7"},
                    "write": True,
                    "purpose": "put the earlier version back",
                },
            ],
            "missing": [],
            "reply_intent": "report what happened to the rollback",
        }
    }


def _reads_of(result: object, operation: str) -> int:
    """How many times the turn called this operation through a connector."""
    return len(
        [
            event
            for event in result.events  # type: ignore[attr-defined]
            if event.kind == "execute"
            and event.data.get("stage") == "call"
            and event.data.get("operation") == operation
        ]
    )


def test_a_state_read_for_another_object_does_not_answer_for_this_one(
    repo_root: Path, tmp_path: Path
) -> None:
    """The record a turn already read is used only when it is the same object.

    The plan reads the deployment of checkout and then asks to roll payments
    back. Checkout's deployment succeeded and a rollback is legal from there;
    payments has already been rolled back and nothing moves out of that state.
    The write names payments, so payments is the object whose state decides,
    and the refusal has to name payments' state rather than checkout's.
    """
    connectors = _ops_connectors(repo_root, tmp_path, payments_state="rolled_back")
    script = [
        {
            "response": {
                "kind": "new_task",
                "runbook_id": "runbook.deployment_rollback",
                "confidence": 0.92,
                "language": "en",
            }
        },
        _rollback_plan(read_for="checkout", rolled_back="payments"),
        {"response": {"reply": "Nothing was sent."}},
    ]
    engine = _engine(repo_root, tmp_path / "state", "ops", script, connectors)

    result = engine.turn("session", "roll payments back to 3.1.7")

    assert [outcome.kind for outcome in result.outcomes] == ["read", "read", "invalid_plan"]
    assert result.pending is None
    assert engine.receipts("session") == []
    issues = result.outcomes[-1].data["issues"]
    assert "the deployment is rolled_back" in issues[0]
    assert "deployment.rollback is not a legal move from there" in issues[0]
    assert _reads_of(result, "deployment.get") == 2


def test_the_record_the_turn_already_read_of_this_object_is_not_read_again(
    repo_root: Path, tmp_path: Path
) -> None:
    """A cached record that is the object the write names is the answer, and stands."""
    connectors = _ops_connectors(repo_root, tmp_path, payments_state="succeeded")
    script = [
        {
            "response": {
                "kind": "new_task",
                "runbook_id": "runbook.deployment_rollback",
                "confidence": 0.92,
                "language": "en",
            }
        },
        _rollback_plan(read_for="payments", rolled_back="payments"),
    ]
    engine = _engine(repo_root, tmp_path / "state", "ops", script, connectors)

    result = engine.turn("session", "roll payments back to 3.1.7")

    assert [outcome.kind for outcome in result.outcomes] == [
        "read",
        "read",
        "approval_pending",
    ]
    assert result.pending is not None
    assert _reads_of(result, "deployment.get") == 1


def test_a_write_the_lifecycle_allows_still_reaches_the_approval_gate(
    repo_root: Path, tmp_path: Path
) -> None:
    script = [
        {
            "response": {
                "kind": "new_task",
                "runbook_id": "runbook.alert_silence",
                "confidence": 0.9,
                "language": "en",
            }
        },
        {
            "response": {
                "runbook_id": "runbook.alert_silence",
                "operations": [
                    {
                        "integration_id": ALERTING,
                        "operation": "alert.silence",
                        "payload": {"alert_id": "alt-9004", "minutes": 30},
                        "write": True,
                        "purpose": "quieten the alert",
                    }
                ],
                "missing": [],
                "reply_intent": "report the silence",
            }
        },
    ]
    engine = _engine(repo_root, tmp_path / "state", "ops", script)
    result = engine.turn("session", "silence alt-9004 for half an hour")
    assert [outcome.kind for outcome in result.outcomes] == ["approval_pending"]
    assert result.pending is not None


# --- what verification adds --------------------------------------------------


def test_verification_fails_when_the_move_did_not_reach_the_state_it_promised(
    registry: SkillRegistry,
) -> None:
    capabilities = CapabilityRegistry(registry)
    verifier = Verifier(capabilities)
    spec = capabilities.operation_spec("integration.deployment_control", "deployment.rollback")
    entity = capabilities.entity_spec("entity.deployment")
    assert spec is not None and entity is not None
    move = entity.moves_from("succeeded")[0]
    reached = ReadBack(
        {
            "deployment": {
                "service": "search",
                "version": "0.8.1",
                "state": "rolled_back",
            }
        }
    )
    status, _ = verifier.verify(
        spec, {"service": "search", "target_version": "0.8.1"}, reached, move=move
    )
    assert status is VerificationStatus.verified
    stuck = ReadBack(
        {
            "deployment": {
                "service": "search",
                "version": "0.8.1",
                "state": "succeeded",
            }
        }
    )
    status, evidence = verifier.verify(
        spec, {"service": "search", "target_version": "0.8.1"}, stuck, move=move
    )
    assert status is VerificationStatus.failed
    assert evidence["state_the_move_promised"] == {
        "expected": "rolled_back",
        "actual": "succeeded",
    }


def test_the_state_is_read_from_the_record_of_the_object_it_is_asked_about(
    registry: SkillRegistry,
) -> None:
    """A read answers about one object, and a list of several answers about none.

    The list branch used to take whatever record came first, whoever was
    asking, so the state of one alert could be reported as the state of
    another. It now reads a list only when a single record answers, or when the
    identity says which record does; and an identity a record carries nothing
    of is not an answer about that object either.
    """
    entity = CapabilityRegistry(registry).entity_spec("entity.alert")
    assert entity is not None
    assert read_state(entity, {"alert": {"state": "firing"}}) == "firing"
    assert read_state(entity, {"alerts": [{"state": "silenced"}]}) == "silenced"
    assert read_state(entity, {"alerts": []}) is None
    assert read_state(None, {"alert": {"state": "firing"}}) is None

    several = {
        "alerts": [
            {"id": "alt-9001", "state": "firing"},
            {"id": "alt-9003", "state": "silenced"},
        ]
    }
    assert read_state(entity, several) is None
    assert read_state(entity, several, {"id": SILENCED_ALERT}) == "silenced"
    assert read_state(entity, several, {"id": "alt-9999"}) is None

    one = {"alert": {"id": "alt-9001", "state": "firing"}}
    assert read_state(entity, one, {"id": "alt-9001"}) == "firing"
    assert read_state(entity, one, {"id": SILENCED_ALERT}) is None
    assert read_state(entity, one, {"service": "search"}) is None


def test_a_state_nobody_reported_is_a_state_nothing_is_legal_from(
    registry: SkillRegistry,
) -> None:
    """An unknown state is not a licence; it is the reason to read before writing."""
    entity = CapabilityRegistry(registry).entity_spec("entity.deployment")
    assert entity is not None
    assert [move.to for move in entity.moves_from("succeeded")] == ["rolled_back"]
    assert entity.moves_from("rolled_back") == []
    assert entity.moves_from(None) == []


# --- what the runtime offers -------------------------------------------------


def test_only_legal_and_offered_moves_are_suggested(
    access_resolver: CapabilityResolver,
) -> None:
    assert access_resolver.suggestions_for_entity("entity.access_request", "open") == [
        "record its outcome (the next step this access_request needs)"
    ]
    assert access_resolver.suggestions_for_entity("entity.access_request", "granted") == []
    assert access_resolver.suggestions_for_entity("entity.access_request", "revoked") == []
    assert access_resolver.suggestions_for_entity("entity.access_request", None) == []


def test_an_entity_without_a_lifecycle_falls_back_to_its_procedures(
    resolver: CapabilityResolver,
) -> None:
    assert resolver.suggestions_for_entity("entity.incident", "open") is None
    assert resolver.suggestions_for_entity("entity.alert", "firing") == ["silence it"]
    assert resolver.suggestions_for_entity("entity.alert", "silenced") == []
    assert resolver.suggestions_for_entity("entity.alert", "resolved") == []


# --- what only a person may fill in ------------------------------------------


def _plan_with(payload: dict, missing: list[str] | None = None) -> Plan:
    return Plan.model_validate(
        {
            "runbook_id": "runbook.incident_note",
            "operations": [
                {
                    "integration_id": "integration.incident_tracker",
                    "operation": "incident.note",
                    "payload": payload,
                    "write": True,
                    "purpose": "write the note",
                }
            ],
            "missing": [
                {"field": field, "question": f"What should {field} be?"}
                for field in missing or []
            ],
            "reply_intent": "report the note",
        }
    )


@pytest.fixture
def note_goal(registry: SkillRegistry, catalog_copy: Path) -> Hydrated:
    """The note procedure, with the note reserved to the person who writes it."""
    target = catalog_copy / "runbook" / "incident_note" / "v1" / "skill.md"
    target.write_text(
        target.read_text(encoding="utf-8").replace(
            "capabilities:", "human_inputs:\n  - note\ncapabilities:"
        ),
        encoding="utf-8",
    )
    return SkillRegistry([catalog_copy]).closure("runbook.incident_note")


def test_a_human_input_the_model_filled_in_is_rejected(note_goal: Hydrated) -> None:
    plan = _plan_with({"incident_id": "inc-501", "note": "probably a bad deploy"})
    issues = validate_plan(plan, note_goal)
    assert len(issues) == 1
    assert "'note' is a human input" in issues[0]
    assert "`missing`" in issues[0]


def test_a_human_input_the_person_gave_is_accepted(note_goal: Hydrated) -> None:
    payload = {"incident_id": "inc-501", "note": "restarted search at 09:12"}
    collected = {"note": "restarted search at 09:12"}
    assert validate_plan(_plan_with(payload), note_goal, collected) == []
    assert validate_plan(_plan_with(payload), note_goal, {"note": "something else"}) != []


def test_a_human_input_left_out_is_asked_for_instead(note_goal: Hydrated) -> None:
    plan = _plan_with({"incident_id": "inc-501"}, missing=["note"])
    assert validate_plan(plan, note_goal) == []


def test_a_field_outside_the_human_inputs_is_left_to_the_model(note_goal: Hydrated) -> None:
    plan = _plan_with({"incident_id": "inc-501", "note": "restarted search"}, missing=["note"])
    issues = validate_plan(plan, note_goal)
    assert [issue for issue in issues if "incident_id" in issue] == []
