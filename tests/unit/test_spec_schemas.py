"""The normative schemas of `spec/schemas/`, held to the code they describe.

Three of them were generated from pydantic contracts and must equal what the
contract produces today. The others describe documents the repository ships
(scenarios, the guardrail catalog) or records the runtime writes (audit events,
plans); each is checked against real ones and, where the code enumerates a
value, against that enumeration. The schemas of the repository root are held
by `test_packaged_defaults.py`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, get_args

import pytest
from jsonschema import Draft202012Validator

from oska.audit.sink import MemorySink
from oska.config import Settings
from oska.contracts.events import AuditEvent, EventKind
from oska.contracts.operation import PlannedOperation
from oska.contracts.plan import MissingInput, Plan
from oska.contracts.platform import PackManifest, PlatformManifest
from oska.engine.factory import build_engine
from oska.governance.guardrails import CHAIN_POINTS, Point
from oska.kit.scaffold import new_pack
from oska.planner.scripted import ScriptedModelProvider

REPO_ROOT = Path(__file__).resolve().parents[2]
SPEC_SCHEMAS = REPO_ROOT / "spec" / "schemas"
DRAFT = "https://json-schema.org/draft/2020-12/schema"
ID_BASE = "https://oska.spec/schemas/"

# The plan schema names the procedure `procedure_id`; the code names it `runbook_id`.
# `language` is the language the planner detected; the schema does not carry it.
PLAN_RENAMES = {"runbook_id": "procedure_id"}
PLAN_CODE_ONLY = {"language"}


def _schema(name: str) -> dict[str, Any]:
    return json.loads((SPEC_SCHEMAS / f"{name}.schema.json").read_text(encoding="utf-8"))


def _validator(name: str) -> Draft202012Validator:
    schema = _schema(name)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER)


def _generated(model: type, name: str) -> dict[str, Any]:
    """What the contract produces, decorated the way the committed file is."""
    schema = model.model_json_schema()  # type: ignore[attr-defined]
    schema["$schema"] = DRAFT
    schema["$id"] = f"{ID_BASE}{name}.schema.json"
    return schema


def _errors(validator: Draft202012Validator, document: Any) -> list[str]:
    return [
        f"{'/'.join(str(part) for part in error.absolute_path) or '<root>'}: {error.message}"
        for error in validator.iter_errors(document)
    ]


SCHEMA_NAMES = sorted(path.name.split(".")[0] for path in SPEC_SCHEMAS.glob("*.json"))


@pytest.mark.parametrize("name", SCHEMA_NAMES)
def test_every_schema_is_a_draft_2020_12_schema_with_its_own_id(name: str) -> None:
    schema = _schema(name)
    Draft202012Validator.check_schema(schema)
    assert schema["$schema"] == DRAFT
    assert schema["$id"] == f"{ID_BASE}{name}.schema.json"


def test_the_readme_names_every_schema_of_the_directory() -> None:
    readme = (SPEC_SCHEMAS / "README.md").read_text(encoding="utf-8")
    for path in sorted(SPEC_SCHEMAS.glob("*.schema.json")):
        assert f"`{path.name}`" in readme, f"{path.name} is not in the README table"


@pytest.mark.parametrize(
    ("name", "model"), [("pack", PackManifest), ("platform", PlatformManifest)]
)
def test_the_manifest_schemas_are_what_their_contracts_generate(name: str, model: type) -> None:
    assert _schema(name) == _generated(model, name), (
        f"spec/schemas/{name}.schema.json drifted from {model.__name__}; regenerate it"
    )


def test_the_manifest_schemas_validate_the_example_manifests(examples: Path) -> None:
    import yaml

    platform = yaml.safe_load((examples / "platform.yaml").read_text(encoding="utf-8"))
    assert _errors(_validator("platform"), platform) == []
    for pack_file in sorted(examples.glob("*/pack.yaml")):
        manifest = yaml.safe_load(pack_file.read_text(encoding="utf-8"))
        assert _errors(_validator("pack"), manifest) == [], pack_file


def _example_scenarios() -> list[Path]:
    return sorted((REPO_ROOT / "examples").glob("*/scenarios/*.json"))


def test_the_reference_scenarios_are_found() -> None:
    assert len(_example_scenarios()) >= 30


@pytest.mark.parametrize(
    "path", _example_scenarios(), ids=lambda path: f"{path.parent.parent.name}/{path.name}"
)
def test_every_example_scenario_validates(path: Path) -> None:
    document = json.loads(path.read_text(encoding="utf-8"))
    assert _errors(_validator("scenario"), document) == []


def test_every_scenario_the_pack_template_ships_validates(tmp_path: Path) -> None:
    target = new_pack("sample", entity="invoice", system="ledger", into=tmp_path)
    shipped = sorted((target / "scenarios").glob("*.json"))
    assert shipped, "the pack template ships no scenarios"
    validator = _validator("scenario")
    for path in shipped:
        document = json.loads(path.read_text(encoding="utf-8"))
        assert "${" not in path.read_text(encoding="utf-8"), f"{path.name} kept a placeholder"
        assert _errors(validator, document) == [], path.name


def test_the_guardrail_catalog_validates() -> None:
    document = json.loads((REPO_ROOT / "configs" / "guardrails.json").read_text(encoding="utf-8"))
    assert _errors(_validator("guardrail-catalog"), document) == []


def test_the_guardrail_catalog_schema_names_the_points_of_the_code() -> None:
    schema = json.dumps(_schema("guardrail-catalog"))
    for point in get_args(Point):
        assert f'"{point}"' in schema, f"point {point} is missing from the guardrail schema"
    assert set(CHAIN_POINTS) <= set(get_args(Point))


def test_the_audit_schema_allows_every_event_kind_the_code_emits() -> None:
    allowed = set(_schema("audit-event")["properties"]["kind"]["enum"])
    assert set(get_args(EventKind)) == allowed


def test_the_audit_schema_names_the_guardrail_points_of_the_code() -> None:
    point = _schema("audit-event")["properties"]["data"]["properties"]["point"]["enum"]
    assert set(point) == set(get_args(Point))


def test_an_event_of_every_kind_validates_as_serialized() -> None:
    validator = _validator("audit-event")
    for kind in get_args(EventKind):
        event = AuditEvent(kind=kind, session_id="s", turn_id="t", at="2026-01-01T00:00:00+00:00")
        assert _errors(validator, event.model_dump(mode="json")) == [], kind


def test_the_envelope_of_an_event_is_closed() -> None:
    validator = _validator("audit-event")
    event = AuditEvent(kind="turn", session_id="s", turn_id="t", at="2026-01-01T00:00:00+00:00")
    assert _errors(validator, {**event.model_dump(mode="json"), "extra": 1})


@pytest.fixture
def written_events(example_settings: Settings) -> list[dict[str, Any]]:
    """The events one write turn and its approval leave, as they would be written to disk."""
    sink = MemorySink()
    script = [
        {"response": {"kind": "new_task", "runbook_id": "runbook.service_restart",
                      "confidence": 0.9, "language": "en"}},
        {"response": {
            "runbook_id": "runbook.service_restart",
            "operations": [
                {"integration_id": "integration.container_runtime", "operation": "service.status",
                 "payload": {"service": "search"}, "write": False, "purpose": "read the state"},
                {"integration_id": "integration.container_runtime", "operation": "service.restart",
                 "payload": {"service": "search"}, "write": True, "purpose": "restart it"},
            ],
            "missing": [],
            "reply_intent": "report the restart",
        }},
    ]
    engine = build_engine(
        example_settings, provider=ScriptedModelProvider(script), audit_sink=sink
    )
    engine.turn("schema-session", "the search service in staging needs a restart, it is wedged")
    engine.turn("schema-session", "yes")
    return [event.model_dump(mode="json") for event in sink.events]


def test_the_events_a_real_turn_writes_validate(written_events: list[dict[str, Any]]) -> None:
    validator = _validator("audit-event")
    kinds = {event["kind"] for event in written_events}
    assert {"turn", "route", "plan", "policy", "approval_requested", "approval_decided",
            "execute", "verify", "reply"} <= kinds
    for event in written_events:
        assert _errors(validator, event) == [], event["kind"]


def test_plan_fields_are_the_schema_fields_after_the_documented_rename() -> None:
    schema = _schema("plan")
    mapped = {PLAN_RENAMES.get(name, name) for name in Plan.model_fields} - PLAN_CODE_ONLY
    assert mapped == set(schema["properties"])
    defs = schema["$defs"]
    assert set(PlannedOperation.model_fields) == set(defs["plannedOperation"]["properties"])
    assert set(MissingInput.model_fields) == set(defs["missingInput"]["properties"])
    required = {
        PLAN_RENAMES.get(name, name)
        for name, field in Plan.model_fields.items()
        if field.is_required()
    }
    assert required == set(schema["required"])


def _as_schema_plan(plan: Plan) -> dict[str, Any]:
    document = plan.model_dump(mode="json")
    for name in PLAN_CODE_ONLY:
        document.pop(name)
    return {PLAN_RENAMES.get(name, name): value for name, value in document.items()}


@pytest.mark.parametrize("procedure", ["runbook.service_restart", "composite.incident_triage"])
def test_a_plan_the_code_builds_validates_after_the_rename(procedure: str) -> None:
    plan = Plan(
        runbook_id=procedure,
        operations=[
            PlannedOperation(
                integration_id="integration.container_runtime",
                operation="service.restart",
                payload={"service": "search"},
                write=True,
                purpose="restart it",
            )
        ],
        missing=[MissingInput(field="service", question="Which service?")],
        reply_intent="report the restart",
        language="en",
    )
    assert _errors(_validator("plan"), _as_schema_plan(plan)) == []
    assert re.match(_schema("plan")["properties"]["procedure_id"]["pattern"], procedure)


def test_a_plan_with_the_code_name_for_the_procedure_does_not_validate() -> None:
    document = Plan(runbook_id="runbook.sample").model_dump(mode="json")
    assert _errors(_validator("plan"), document)
