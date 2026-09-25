"""What a turn asks a presentation surface to show, and where every value comes from.

A directive is declared by a procedure for a moment of the turn and filled from
evidence alone: records a read returned, the record the turn selected, a
verified receipt. What a person typed is not evidence, and the catalog refuses a
binding that asks for it. A binding the evidence cannot answer drops the whole
directive. A domain that does not enable presentation gets none. The catalog
refuses a binding that names nothing.

A write that did not land ends the goal where it stands: nothing planned after
it runs, no later procedure is planned, and no completion view is shown.

The unsupported reply lives here too, because it is the other thing a turn
shows about itself: it is written in the conversation's language by the one
reply call a turn makes, from the scope the catalog declares, and falls back to
the template when no text comes back.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

import oska.engine.factory as factory
from oska.audit.sink import MemorySink
from oska.catalog.registry import SkillRegistry
from oska.catalog.validator import validate
from oska.config import Settings
from oska.contracts.common import Message, Outcome, PendingGoal
from oska.contracts.skill import PresentationMoment, Skill, SubjectSpec
from oska.engine.presentation import (
    MOMENTS,
    Evidence,
    _completed,
    _verified_receipt,
    bind,
    turn_moment,
)
from oska.engine.subject import Referent, Shown
from oska.planner.provider import ModelProviderError
from oska.planner.scripted import ScriptedModelProvider
from oska.router.model_router import ROUTE_SCHEMA

REPO_ROOT = Path(__file__).resolve().parents[2]
SCENARIOS = REPO_ROOT / "examples" / "ops" / "scenarios"
WITH_DIRECTIVES = (
    "27_subject_from_what_was_shown",
    "28_directive_from_verified_write",
    "29_comparative_on_shown_rows",
)
RESTART = "runbook.service_restart"


class Counter:
    """A scripted provider that counts completions and keeps each last user message."""

    name = "counter"

    def __init__(self, script: list[dict]) -> None:
        self._inner = ScriptedModelProvider(script)
        self.completions = 0
        self.briefs: list[str] = []

    def complete_json(
        self,
        system: str,
        messages: list[Message],
        schema: dict,
        *,
        temperature: float = 0.0,
    ) -> dict:
        self.completions += 1
        said = [message.content for message in messages if message.role == "user"]
        self.briefs.append(said[-1] if said else "")
        return self._inner.complete_json(system, messages, schema, temperature=temperature)

    def embed(self, texts: list[str]) -> list[list[float]]:
        return self._inner.embed(texts)


def _receipt(verification: str = "verified") -> Outcome:
    return Outcome(
        kind="receipt",
        integration_id="integration.container_runtime",
        operation="service.restart",
        receipt_id="r-1",
        data={
            "status": "executed",
            "verification": verification,
            "payload": {"service": "search"},
            "response": {"service": {"name": "search", "status": "running"}},
        },
    )


def _run(
    settings: Settings, name: str, *, presentation: bool = False
) -> list[tuple[list[dict], dict]]:
    """Replay a scenario, with presentation on when a domain that enables it is asked for."""
    scenario = json.loads((SCENARIOS / f"{name}.json").read_text(encoding="utf-8"))
    engine = factory.build_engine(
        settings,
        provider=ScriptedModelProvider(scenario["provider_script"]),
        audit_sink=MemorySink(),
        presentation=presentation,
    )
    ran = []
    for turn in scenario["turns"]:
        result = engine.turn(name, turn["user"])
        ran.append(([d.model_dump(mode="json") for d in result.directives], turn["expect"]))
        reply = [event for event in result.events if event.kind == "reply"][-1]
        assert reply.data.get("directives", []) == ran[-1][0]
    return ran


def _matches(produced: list[dict], expected: list[dict]) -> bool:
    """Each expected directive, in order, names the same view and a subset of its fields."""
    if len(produced) != len(expected):
        return False
    for made, wanted in zip(produced, expected, strict=True):
        for key in ("procedure", "moment", "view"):
            if made[key] != wanted[key]:
                return False
        if any(made["fields"].get(name) != value for name, value in wanted["fields"].items()):
            return False
    return True


# Binding a declared moment to evidence


def test_every_kind_of_binding_is_answered_from_evidence() -> None:
    declared = PresentationMoment(
        view="card",
        bind={
            "names": "$items.name",
            "picked": "$selection",
            "receipt": "$receipt.receipt_id",
            "service": "$receipt.service",
            "label": "restarted",
        },
        fields={"kind": "service"},
    )
    evidence = Evidence(
        records=[{"name": "search"}, {"name": "payments"}],
        receipt=_receipt(),
        selection=["search"],
    )
    directive = bind(RESTART, "on_verified_write", declared, evidence)
    assert directive is not None
    assert directive.fields == {
        "kind": "service",
        "names": ["search", "payments"],
        "picked": "search",
        "receipt": "r-1",
        "service": "search",
        "label": "restarted",
    }


@pytest.mark.parametrize(
    "binding",
    [
        "$item.name",  # two records were read, so there is no one record
        "$items.version",  # no record carries it
        "$inputs.service",  # what a person typed is not evidence
        "$receipt.replicas",  # the payload does not carry it
        "$selection",  # nothing was selected
        "$nothing.at_all",  # not a kind of evidence
    ],
)
def test_a_binding_the_evidence_cannot_answer_drops_the_directive(binding: str) -> None:
    declared = PresentationMoment(view="card", bind={"shown": "$items.name", "extra": binding})
    evidence = Evidence(
        records=[{"name": "search"}, {"name": "payments"}],
        receipt=_receipt(),
    )
    assert bind(RESTART, "on_results", declared, evidence) is None


def test_a_receipt_binding_needs_a_verified_receipt() -> None:
    writes = {"service.restart"}
    assert _verified_receipt([_receipt("unverified")], writes) is None
    assert _verified_receipt([_receipt("failed")], writes) is None
    assert _verified_receipt([_receipt()], writes) is not None
    declared = PresentationMoment(view="done", bind={"service": "$receipt.service"})
    assert bind(RESTART, "on_verified_write", declared, Evidence()) is None
    assert Evidence(receipt=None).moments() == []


def test_the_moments_are_taken_in_their_declared_order() -> None:
    evidence = Evidence(records=[{}], receipt=_receipt(), selection=["x"], completed=True)
    assert evidence.moments() == [
        "on_verified_write",
        "on_selection",
        "on_single_result",
        "on_completion",
    ]
    assert MOMENTS[0] == "on_verified_write"


def test_the_moment_a_turn_presents_at_needs_the_selection_to_be_what_it_showed() -> None:
    picked = Referent("entity.service", [{"name": "search"}], "focus")
    shown = Shown("entity.service", "i", "service.status", [{"name": "search"}])
    other = Shown("entity.service", "i", "service.status", [{"name": "payments"}])
    assert turn_moment(shown, picked, False) == "on_selection"
    assert turn_moment(other, picked, False) == "on_single_result"
    assert turn_moment(None, None, True) == "on_completion"


# The turn, end to end


@pytest.mark.parametrize("name", WITH_DIRECTIVES)
def test_the_scenarios_emit_the_directives_they_expect(
    example_settings: Settings, name: str
) -> None:
    for produced, expect in _run(example_settings, name, presentation=True):
        if "directives" in expect:
            assert _matches(produced, expect["directives"]), (produced, expect["directives"])


def test_a_verified_write_fills_its_directive_from_the_receipt(
    example_settings: Settings,
) -> None:
    ran = _run(example_settings, "28_directive_from_verified_write", presentation=True)
    directive = ran[-1][0][0]
    assert directive["moment"] == "on_verified_write"
    assert directive["fields"]["verification"] == "verified"
    assert directive["fields"]["receipt"]


def test_no_directive_when_presentation_is_off(example_settings: Settings) -> None:
    for produced, _ in _run(example_settings, "27_subject_from_what_was_shown"):
        assert produced == []


def _turns(settings: Settings, name: str, session_id: str, *, presentation: bool) -> list:
    """Replay a scenario end to end, keeping every turn's full result.

    Each call needs its own session id: two engines built on the same settings
    share the state backend, and a second run over the session the first one
    left behind would not be the same turns run again.
    """
    scenario = json.loads((SCENARIOS / f"{name}.json").read_text(encoding="utf-8"))
    engine = factory.build_engine(
        settings,
        provider=ScriptedModelProvider(scenario["provider_script"]),
        audit_sink=MemorySink(),
        presentation=presentation,
    )
    return [engine.turn(session_id, turn["user"]) for turn in scenario["turns"]]


_VOLATILE_IDS = ("approval_id", "receipt_id")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _scrubbed(value: object) -> object:
    """`value`, with the ids a fresh run mints anew replaced by a constant.

    An approval id is minted per turn and never asked to repeat, so two
    otherwise identical runs disagree on it alone; everything else a turn
    produced is compared as it stands.
    """
    if isinstance(value, dict):
        return {
            key: ("scrubbed" if key in _VOLATILE_IDS else _scrubbed(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_scrubbed(item) for item in value]
    return value


def test_a_directive_changes_nothing_a_turn_did(example_settings: Settings) -> None:
    """OSKA-IXN-23: the same turns, with presentation on and off, differ only in the
    directives. The reply, the outcomes and what is left pending are identical.
    """
    name = "28_directive_from_verified_write"
    on = _turns(example_settings, name, "presentation-on", presentation=True)
    off = _turns(example_settings, name, "presentation-off", presentation=False)
    assert any(result.directives for result in on)
    assert all(result.directives == [] for result in off)
    assert len(on) == len(off)
    for with_directives, without in zip(on, off, strict=True):
        assert _UUID.sub("id", with_directives.reply) == _UUID.sub("id", without.reply)
        assert _scrubbed(with_directives.model_dump(mode="json")["outcomes"]) == _scrubbed(
            without.model_dump(mode="json")["outcomes"]
        )
        assert _scrubbed(with_directives.model_dump(mode="json")["pending"]) == _scrubbed(
            without.model_dump(mode="json")["pending"]
        )


# The catalog refuses a declaration that names nothing


def _codes(registry: SkillRegistry, changed: Skill, schemas_dir: Path) -> set[str]:
    skills = [changed if skill.id == changed.id else skill for skill in registry.skills()]
    return {issue.code for issue in validate(changed, skills, schema_dir=schemas_dir)}


@pytest.mark.parametrize(
    ("binding", "code"),
    [
        ("$bogus.name", "presentation_unknown_evidence"),
        ("$items", "presentation_malformed_binding"),
        ("$selection.name", "presentation_malformed_binding"),
        ("$item.rule", "presentation_unknown_field"),
        ("$inputs.service", "presentation_unverified_input"),
        ("$receipt.colour", "presentation_unknown_receipt_field"),
    ],
)
def test_a_binding_that_names_no_evidence_is_refused(
    registry: SkillRegistry, schemas_dir: Path, binding: str, code: str
) -> None:
    restart = registry.get(RESTART).model_copy(deep=True)
    restart.frontmatter.presentation = {
        "on_results": PresentationMoment(view="v", bind={"x": binding})
    }
    assert code in _codes(registry, restart, schemas_dir)


def test_the_example_declarations_hold(registry: SkillRegistry, schemas_dir: Path) -> None:
    restart = registry.get(RESTART)
    assert not {
        code for code in _codes(registry, restart, schemas_dir) if code.startswith("presentation")
    }


def test_a_receipt_binding_on_a_procedure_that_writes_nothing_is_a_warning(
    registry: SkillRegistry, schemas_dir: Path
) -> None:
    health = registry.get("runbook.service_health").model_copy(deep=True)
    health.frontmatter.presentation = {
        "on_completion": PresentationMoment(view="v", bind={"x": "$receipt.receipt_id"})
    }
    assert "presentation_receipt_without_write" in _codes(registry, health, schemas_dir)
    skills = [health if skill.id == health.id else skill for skill in registry.skills()]
    issues = validate(health, skills, schema_dir=schemas_dir)
    assert {issue.severity for issue in issues if issue.code.startswith("presentation")} == {
        "warning"
    }


def test_a_subject_names_an_entity_the_procedure_can_name(
    registry: SkillRegistry, schemas_dir: Path
) -> None:
    restart = registry.get(RESTART).model_copy(deep=True)
    restart.frontmatter.subject = SubjectSpec(entity="entity.nowhere", joins="focus")
    assert "subject_unknown_entity" in _codes(registry, restart, schemas_dir)
    restart.frontmatter.subject = SubjectSpec(entity="entity.incident", joins="focus")
    codes = _codes(registry, restart, schemas_dir)
    assert {"subject_unlisted_entity", "subject_unjoinable"} <= codes


def test_an_entity_shows_and_compares_only_fields_it_declares(
    registry: SkillRegistry, schemas_dir: Path
) -> None:
    service = registry.get("entity.service").model_copy(deep=True)
    service.frontmatter.display_fields = ["name", "colour"]
    service.frontmatter.comparable_fields = ["weight"]
    codes = _codes(registry, service, schemas_dir)
    assert {
        "presentation_unknown_display_field",
        "presentation_unknown_comparable_field",
    } <= codes


# The unsupported reply


def _off_scope(language: str = "it") -> dict:
    return {
        "expect_contains": "prenotami un volo",
        "response": {
            "kind": "off_scope",
            "runbook_id": None,
            "confidence": 0.95,
            "language": language,
        },
    }


def test_the_unsupported_reply_is_written_by_the_one_reply_call(
    example_settings: Settings,
) -> None:
    provider = Counter(
        [
            _off_scope(),
            {
                "expect_contains": "\"facts\": [\"I can check the health of a service",
                "response": {"reply": "Non posso farlo qui. Posso controllare un servizio."},
            },
        ]
    )
    engine = factory.build_engine(example_settings, provider=provider, audit_sink=MemorySink())
    result = engine.turn("s", "prenotami un volo per Lisbona")
    assert result.reply == "Non posso farlo qui. Posso controllare un servizio."
    assert provider.completions == 2  # the classification and the one reply call
    assert '"outcomes": []' in provider.briefs[-1]


def test_the_unsupported_reply_falls_back_to_the_template(example_settings: Settings) -> None:
    provider = Counter([_off_scope("en")])
    engine = factory.build_engine(example_settings, provider=provider, audit_sink=MemorySink())
    result = engine.turn("s", "prenotami un volo per Lisbona")
    assert result.reply.startswith("That is not something I can do here.")
    assert "check the health of a service" in result.reply
    assert provider.completions == 2  # the reply call was made, found nothing, added nothing


# A goal stops at a write that did not land

RUNTIME = "integration.container_runtime"
_STOPPED = {
    "service": {
        "name": "reporting",
        "environment": "staging",
        "status": "stopped",
        "version": "4.0.0",
        "replicas": 1,
        "restarts": 7,
    }
}


class Dispatching:
    """A provider that routes to one procedure, plans from a queue and writes no reply.

    Each answer is picked by the schema it is asked for, so a turn that plans
    more, or less, than the test expects is counted instead of being handed the
    answer meant for another call.
    """

    name = "dispatching"

    def __init__(self, runbook_id: str, plans: list[dict]) -> None:
        self._runbook_id = runbook_id
        self._plans = list(plans)
        self.planned = 0

    def complete_json(
        self,
        system: str,
        messages: list[Message],
        schema: dict,
        *,
        temperature: float = 0.0,
    ) -> dict:
        if schema is ROUTE_SCHEMA:
            return {
                "kind": "new_task",
                "runbook_id": self._runbook_id,
                "confidence": 0.9,
                "language": "en",
            }
        if "operations" in schema.get("properties", {}) and self._plans:
            self.planned += 1
            return self._plans.pop(0)
        raise ModelProviderError("nothing scripted for this call")

    def embed(self, texts: list[str]) -> list[list[float]]:
        return ScriptedModelProvider([]).embed(texts)


def _operation(name: str, *, write: bool = False) -> dict:
    return {
        "integration_id": RUNTIME,
        "operation": name,
        "payload": {"service": "reporting"},
        "write": write,
        "purpose": name,
    }


def _plan(runbook_id: str, operations: list[dict]) -> dict:
    return {
        "runbook_id": runbook_id,
        "operations": operations,
        "missing": [],
        "reply_intent": "report what was done",
    }


def _frontmatter(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    _, header, body = text.split("---\n", 2)
    return yaml.safe_load(header), body


def _catalog(
    catalog_copy: Path, *, on_completion: bool = False, composite: bool = False
) -> Path:
    """The example catalog, with the restart declaring a completion view or joined in a goal."""
    restart = catalog_copy / "runbook" / "service_restart" / "v1" / "skill.md"
    if on_completion:
        header, body = _frontmatter(restart)
        header["presentation"]["on_completion"] = {
            "view": "restart_finished",
            "fields": {"finished": True},
        }
        dumped = yaml.safe_dump(header, sort_keys=False, allow_unicode=True)
        restart.write_text(f"---\n{dumped}---\n{body}", encoding="utf-8")
    if composite:
        header, body = _frontmatter(
            catalog_copy / "composite" / "incident_triage" / "v1" / "skill.md"
        )
        header.update(
            id="composite.restart_then_check",
            description="Restarts a service, then reads its health to see it came back.",
            triggers=[
                "restart it and check it came back",
                "bounce the service then check its health",
                "restart and verify the service",
            ],
            runbooks=[RESTART, "runbook.service_health"],
        )
        header["runtime"] = {"action": "restart a service and check it", "order": 9}
        target = catalog_copy / "composite" / "restart_then_check" / "v1"
        target.mkdir(parents=True)
        dumped = yaml.safe_dump(header, sort_keys=False, allow_unicode=True)
        (target / "skill.md").write_text(f"---\n{dumped}---\n{body}", encoding="utf-8")
    return catalog_copy


def _engine(settings: Settings, skills: Path, provider: Dispatching, *, presentation: bool):
    return factory.build_engine(
        settings.model_copy(update={"skills_roots": [skills]}),
        provider=provider,
        audit_sink=MemorySink(),
        presentation=presentation,
    )


def _approve_a_write_that_is_not_confirmed(engine, session: str) -> object:
    """Ask for the restart, then approve it while the read-back reports the service stopped."""
    first = engine.turn(session, "reporting is wedged, restart it and check it")
    assert first.pending is not None and first.pending.kind == "approval"
    engine.connectors.get(RUNTIME).set_behavior("service.status", {"override": _STOPPED})
    return engine.turn(session, "yes")


def test_nothing_planned_after_an_unconfirmed_write_runs(
    example_settings: Settings, catalog_copy: Path
) -> None:
    provider = Dispatching(
        RESTART,
        [
            _plan(
                RESTART,
                [
                    _operation("service.status"),
                    _operation("service.restart", write=True),
                    _operation("service.status"),
                ],
            )
        ],
    )
    engine = _engine(example_settings, _catalog(catalog_copy), provider, presentation=False)
    result = _approve_a_write_that_is_not_confirmed(engine, "unconfirmed")
    assert [outcome.kind for outcome in result.outcomes] == ["receipt"]
    assert result.outcomes[0].data["verification"] == "failed"
    assert result.pending is None
    assert engine.state("unconfirmed").pending_goal is None


def test_a_composite_does_not_go_on_past_an_unconfirmed_write(
    example_settings: Settings, catalog_copy: Path
) -> None:
    provider = Dispatching(
        "composite.restart_then_check",
        [
            _plan(
                RESTART,
                [_operation("service.status"), _operation("service.restart", write=True)],
            ),
            _plan("runbook.service_health", [_operation("service.status")]),
        ],
    )
    engine = _engine(
        example_settings, _catalog(catalog_copy, composite=True), provider, presentation=False
    )
    result = _approve_a_write_that_is_not_confirmed(engine, "composite")
    hydrated = [event.data["runbook_id"] for event in result.events if event.kind == "hydrate"]
    assert hydrated == []
    assert provider.planned == 1
    assert [outcome.kind for outcome in result.outcomes] == ["receipt"]
    assert engine.state("composite").pending_goal is None


def test_a_composite_does_not_go_on_past_an_unconfirmed_write_found_in_the_ledger(
    example_settings: Settings, catalog_copy: Path
) -> None:
    restart = _plan(
        RESTART, [_operation("service.status"), _operation("service.restart", write=True)]
    )
    provider = Dispatching(
        "composite.restart_then_check",
        [restart, restart, _plan("runbook.service_health", [_operation("service.status")])],
    )
    engine = _engine(
        example_settings, _catalog(catalog_copy, composite=True), provider, presentation=False
    )
    _approve_a_write_that_is_not_confirmed(engine, "again")
    result = engine.turn("again", "reporting is wedged, restart it and check it")
    assert [outcome.kind for outcome in result.outcomes] == ["read", "ledger_hit"]
    hydrated = [event.data["runbook_id"] for event in result.events if event.kind == "hydrate"]
    assert hydrated == [RESTART]
    assert provider.planned == 2
    assert engine.state("again").pending_goal is None


def _replay_then_another_restart() -> dict:
    """A read, the reporting restart already in the ledger, then a restart of search."""
    another = {**_operation("service.restart", write=True), "payload": {"service": "search"}}
    return _plan(
        RESTART,
        [_operation("service.status"), _operation("service.restart", write=True), another],
    )


def _restart_reporting() -> dict:
    return _plan(
        RESTART, [_operation("service.status"), _operation("service.restart", write=True)]
    )


def test_a_write_found_unconfirmed_in_the_ledger_stops_the_rest_of_its_plan(
    example_settings: Settings, catalog_copy: Path
) -> None:
    provider = Dispatching(
        "composite.restart_then_check", [_restart_reporting(), _replay_then_another_restart()]
    )
    engine = _engine(
        example_settings, _catalog(catalog_copy, composite=True), provider, presentation=False
    )
    _approve_a_write_that_is_not_confirmed(engine, "stop-here")

    result = engine.turn("stop-here", "reporting is wedged, restart it and check it")

    assert [outcome.kind for outcome in result.outcomes] == ["read", "ledger_hit"]
    assert result.outcomes[1].data["verification"] == "failed"
    assert [event for event in result.events if event.kind == "approval_requested"] == []
    assert result.pending is None
    state = engine.state("stop-here")
    assert state.pending_approval is None
    assert state.pending_goal is None
    hydrated = [event.data["runbook_id"] for event in result.events if event.kind == "hydrate"]
    assert hydrated == [RESTART]
    assert provider.planned == 2


def test_a_write_found_confirmed_in_the_ledger_lets_its_plan_go_on(
    example_settings: Settings, catalog_copy: Path
) -> None:
    provider = Dispatching(
        "composite.restart_then_check",
        [
            _restart_reporting(),
            _plan("runbook.service_health", [_operation("service.status")]),
            _replay_then_another_restart(),
        ],
    )
    engine = _engine(
        example_settings, _catalog(catalog_copy, composite=True), provider, presentation=False
    )
    asked = engine.turn("go-on", "reporting is wedged, restart it and check it")
    assert asked.pending is not None and asked.pending.kind == "approval"
    done = engine.turn("go-on", "yes")
    assert done.outcomes[0].data["verification"] == "verified"

    result = engine.turn("go-on", "reporting is wedged, restart it and check it")

    kinds = [outcome.kind for outcome in result.outcomes]
    assert kinds == ["read", "ledger_hit", "approval_pending"]
    assert result.outcomes[1].data["verification"] == "verified"
    assert result.pending is not None and result.pending.kind == "approval"
    assert result.pending.approval.exact_payload == {"service": "search"}
    goal = engine.state("go-on").pending_goal
    assert goal is not None and goal.step_index == 0
    assert provider.planned == 3


def test_no_completion_view_follows_a_write_that_did_not_land(
    example_settings: Settings, catalog_copy: Path
) -> None:
    provider = Dispatching(
        RESTART,
        [_plan(RESTART, [_operation("service.status"), _operation("service.restart", write=True)])],
    )
    engine = _engine(
        example_settings, _catalog(catalog_copy, on_completion=True), provider, presentation=True
    )
    result = _approve_a_write_that_is_not_confirmed(engine, "no-completion")
    assert result.outcomes[-1].data["verification"] == "failed"
    assert result.directives == []
    reply = [event for event in result.events if event.kind == "reply"][-1]
    assert "directives" not in reply.data


def test_a_completion_view_needs_every_write_of_the_turn_confirmed() -> None:
    goal = PendingGoal(runbook_ids=[RESTART], step_index=1, collected_inputs={}, missing=[])
    assert _completed(goal, [_receipt()])
    assert not _completed(goal, [_receipt("unverified")])
    assert not _completed(goal, [_receipt("failed")])
    never_left = Outcome(kind="unavailable", operation="service.restart", data={"write": True})
    assert not _completed(goal, [never_left])
    assert not _completed(goal.model_copy(update={"step_index": 0}), [_receipt()])
