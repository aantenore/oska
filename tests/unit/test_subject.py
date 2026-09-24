"""What a conversation is about: taken from what was shown, and nothing else.

A record shown by a read becomes the focus. A follow-up refers back to it in a
fixed precedence: a literal identifier, then the display fields, where the
record in focus sets the score to beat, then the standing focus. A procedure
that joins the focus receives the identifier as a collected input, and the
planner is told it apart from what the person typed. A comparative is handed
only the refinements the shown records actually differ on.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from oska.audit.sink import MemorySink
from oska.catalog.registry import SkillRegistry
from oska.config import Settings
from oska.connectors.mock import MockConnector
from oska.contracts.common import Message, Outcome
from oska.engine import subject
from oska.engine.factory import build_engine
from oska.engine.state import SessionState
from oska.planner import prompts
from oska.planner.scripted import ScriptedModelProvider

CONTAINER_RUNTIME = "integration.container_runtime"
ALERTING = "integration.alerting"
RESTART = "runbook.service_restart"
HEALTH = "runbook.service_health"
SILENCE = "runbook.alert_silence"

ALERTS = [
    {"id": "alt-1", "service": "payments", "rule": "HighErrorRate", "state": "firing"},
    {"id": "alt-2", "service": "checkout", "rule": "CertificateExpiry", "state": "firing"},
    {"id": "alt-3", "service": "notifications", "rule": "QueueBacklog", "state": "silenced"},
]


class Capturing:
    """A scripted provider that keeps every conversation it was handed."""

    name = "capturing"

    def __init__(self, script: list[dict]) -> None:
        self._inner = ScriptedModelProvider(script)
        self.conversations: list[list[Message]] = []

    def complete_json(
        self,
        system: str,
        messages: list[Message],
        schema: dict,
        *,
        temperature: float = 0.0,
    ) -> dict:
        self.conversations.append(list(messages))
        return self._inner.complete_json(system, messages, schema, temperature=temperature)

    def embed(self, texts: list[str]) -> list[list[float]]:
        return self._inner.embed(texts)


def _read(integration: str, operation: str, data: dict) -> Outcome:
    return Outcome(
        kind="read",
        integration_id=integration,
        operation=operation,
        data={"ok": True, "data": data},
    )


def _shown_alerts(registry: SkillRegistry, alerts: list[dict] | None = None) -> SessionState:
    state = SessionState(session_id="s", turn_count=1)
    shown = subject.shown_in(
        [_read(ALERTING, "alert.list", {"alerts": alerts or ALERTS})], registry
    )
    subject.record(state, registry, shown, "on_results")
    return state


def _route(text: str, runbook_id: str) -> dict:
    return {
        "expect_contains": text,
        "response": {
            "kind": "new_task",
            "runbook_id": runbook_id,
            "confidence": 0.9,
            "language": "en",
        },
    }


def _plan(runbook_id: str, operations: list[dict]) -> dict:
    return {
        "response": {
            "runbook_id": runbook_id,
            "operations": operations,
            "missing": [],
            "reply_intent": "report",
        }
    }


def _op(name: str, payload: dict, write: bool = False) -> dict:
    return {
        "integration_id": CONTAINER_RUNTIME,
        "operation": name,
        "payload": payload,
        "write": write,
        "purpose": "step",
    }


# What was shown, and the focus it leaves


def test_a_read_of_one_record_puts_that_record_in_focus(registry: SkillRegistry) -> None:
    state = SessionState(session_id="s", turn_count=1)
    record = {"name": "search", "environment": "staging", "status": "running", "version": "1"}
    shown = subject.shown_in(
        [_read(CONTAINER_RUNTIME, "service.status", {"service": record})], registry
    )
    subject.record(state, registry, shown, "on_single_result")
    assert state.presented["moment"] == "on_single_result"
    assert state.presented["records"][0]["identifier"] == {"name": "search"}
    assert state.presented["records"][0]["display"]["environment"] == "staging"
    assert state.focus["cardinality"] == "one"
    assert state.focus["identifiers"] == [{"name": "search"}]


def test_a_list_read_puts_the_list_in_focus(registry: SkillRegistry) -> None:
    state = _shown_alerts(registry)
    assert state.focus["cardinality"] == "many"
    assert [ids["id"] for ids in state.focus["identifiers"]] == ["alt-1", "alt-2", "alt-3"]


def test_a_turn_that_shows_nothing_leaves_the_focus_as_it_stands(
    registry: SkillRegistry,
) -> None:
    state = _shown_alerts(registry)
    before = (dict(state.presented), dict(state.focus))
    subject.record(state, registry, None, "")
    assert (state.presented, state.focus) == before


def test_a_selection_that_showed_nothing_narrows_the_focus(registry: SkillRegistry) -> None:
    state = _shown_alerts(registry)
    picked = subject.Referent("entity.alert", [{"id": "alt-2"}], "display")
    subject.record(state, registry, None, "on_selection", picked)
    assert state.focus["identifiers"] == [{"id": "alt-2"}]
    assert state.focus["cardinality"] == "one"
    assert len(state.presented["records"]) == 3


def test_a_write_that_was_not_verified_shows_nothing(registry: SkillRegistry) -> None:
    receipt = Outcome(
        kind="receipt",
        integration_id=CONTAINER_RUNTIME,
        operation="service.restart",
        data={"verification": "unverified", "response": {"service": {"name": "search"}}},
    )
    assert subject.shown_in([receipt], registry) is None
    verified = receipt.model_copy(
        update={"data": {**receipt.data, "verification": "verified"}}
    )
    shown = subject.shown_in([verified], registry)
    assert shown is not None and shown.verified_write
    assert shown.records == [{"name": "search"}]


# The precedence a reference is resolved in


def test_a_literal_identifier_wins_over_display_fields_and_focus(
    registry: SkillRegistry,
) -> None:
    state = _shown_alerts(registry)
    alert = registry.get("entity.alert")
    found = subject.resolve("the checkout alert, alt-3", state.presented, state.focus, alert)
    assert found is not None
    assert (found.source, found.identifiers) == ("identifier", [{"id": "alt-3"}])


def test_the_best_display_overlap_picks_one_record(registry: SkillRegistry) -> None:
    state = _shown_alerts(registry)
    alert = registry.get("entity.alert")
    found = subject.resolve("silence the checkout one", state.presented, state.focus, alert)
    assert found is not None
    assert (found.source, found.identifiers) == ("display", [{"id": "alt-2"}])


def test_synonyms_of_the_pack_are_read_before_the_overlap(registry: SkillRegistry) -> None:
    state = _shown_alerts(registry)
    alert = registry.get("entity.alert")
    said = "silence the one for the shop front"
    assert subject.resolve(said, state.presented, state.focus, alert).source == "focus"
    found = subject.resolve(
        said, state.presented, state.focus, alert, {"shop front": "checkout"}
    )
    assert found.identifiers == [{"id": "alt-2"}]


def test_a_challenger_that_only_ties_the_focus_does_not_take_it(
    registry: SkillRegistry,
) -> None:
    state = _shown_alerts(registry)
    state.focus = {**state.focus, "cardinality": "one", "identifiers": [{"id": "alt-1"}]}
    alert = registry.get("entity.alert")
    # alt-1 and alt-2 both mention "firing"; alt-1 is in focus, so alt-2 wins nothing.
    found = subject.resolve("the firing one", state.presented, state.focus, alert)
    assert (found.source, found.identifiers) == ("focus", [{"id": "alt-1"}])
    beaten = subject.resolve("the firing one on checkout", state.presented, state.focus, alert)
    assert (beaten.source, beaten.identifiers) == ("display", [{"id": "alt-2"}])


def test_two_challengers_that_tie_leave_the_standing_focus(registry: SkillRegistry) -> None:
    state = _shown_alerts(registry)
    alert = registry.get("entity.alert")
    found = subject.resolve("the firing ones", state.presented, state.focus, alert)
    assert found.source == "focus"
    assert len(found.identifiers) == 3


def test_nothing_shown_of_the_entity_answers_with_no_referent(registry: SkillRegistry) -> None:
    assert subject.resolve("restart it", {}, {}, registry.get("entity.service")) is None


# Fields with several values


def test_a_field_with_several_values_is_matched_by_membership() -> None:
    access = SkillRegistry([Path(__file__).resolve().parents[2] / "examples/access/skills"])
    user = access.get("entity.user")
    state = SessionState(session_id="s", turn_count=1)
    users = [
        {"username": "amoreau", "display_name": "Alba", "groups": ["finance-reports"]},
        {"username": "jokafor", "display_name": "Joy", "groups": ["release-managers", "finance"]},
    ]
    shown = subject.Shown("entity.user", "integration.directory", "user.get", users)
    subject.record(state, access, shown, "on_results")
    found = subject.resolve("the one in finance", state.presented, state.focus, user)
    assert found.identifiers == [{"username": "jokafor"}]
    whole = subject.resolve("the one in finance-reports", state.presented, state.focus, user)
    assert whole.identifiers == [{"username": "amoreau"}]


def test_the_mock_filters_a_field_with_several_values_by_membership(tmp_path: Path) -> None:
    fixture = tmp_path / "users.json"
    fixture.write_text(
        json.dumps(
            {
                "users": [
                    {"username": "a", "groups": ["finance-reports"]},
                    {"username": "b", "groups": ["finance", "ops"]},
                ]
            }
        ),
        encoding="utf-8",
    )
    connector = MockConnector(
        "integration.directory",
        fixture,
        {"user.list": {"collection": "users", "filter": ["groups"], "as": "users"}},
    )
    result = connector.execute("user.list", {"groups": "finance"})
    assert result.ok
    assert [user["username"] for user in result.data["users"]] == ["b"]


# Joining the focus


def test_a_procedure_that_joins_the_focus_collects_the_identifier(
    registry: SkillRegistry,
) -> None:
    state = SessionState(session_id="s", turn_count=1)
    shown = subject.shown_in(
        [_read(CONTAINER_RUNTIME, "service.status", {"service": {"name": "search"}})], registry
    )
    subject.record(state, registry, shown, "on_single_result")
    inputs, selection, joined = subject.join(
        [registry.get(RESTART)], registry, state, "restart it"
    )
    assert inputs == {"service": "search"}
    assert joined == [RESTART]
    assert selection.source == "focus"
    assert subject.focus_inputs(registry.get(RESTART), registry, state, inputs) == inputs
    assert subject.focus_inputs(registry.get(RESTART), registry, state, {"service": "x"}) == {}


def test_a_client_supplied_context_does_not_override_the_focus(
    registry: SkillRegistry,
) -> None:
    """OSKA-COG-27: a claim naming a record nothing showed is not taken as coming from
    the focus, and it never becomes the focus either, only a governed read does that.
    """
    state = SessionState(session_id="s", turn_count=1)
    shown = subject.shown_in(
        [_read(CONTAINER_RUNTIME, "service.status", {"service": {"name": "search"}})], registry
    )
    subject.record(state, registry, shown, "on_single_result")
    still_search = dict(state.focus)

    # A client claims the subject is "payments", a record no read of this turn showed.
    claimed = subject.focus_inputs(registry.get(RESTART), registry, state, {"service": "payments"})
    assert claimed == {}, "a claim not backed by evidence is not taken as the focus input"
    assert state.focus == still_search, "the claim alone never moves the focus"

    # Only a governed read of "payments" makes it what was shown, and then the focus.
    now_payments = subject.shown_in(
        [_read(CONTAINER_RUNTIME, "service.status", {"service": {"name": "payments"}})], registry
    )
    subject.record(state, registry, now_payments, "on_single_result")
    assert state.focus["identifiers"] == [{"name": "payments"}]
    assert subject.focus_inputs(
        registry.get(RESTART), registry, state, {"service": "payments"}
    ) == {"service": "payments"}


def test_a_list_in_focus_is_not_guessed_down_to_one(registry: SkillRegistry) -> None:
    state = _shown_alerts(registry)
    inputs, selection, joined = subject.join(
        [registry.get(SILENCE)], registry, state, "silence it"
    )
    assert (inputs, selection, joined) == ({}, None, [])
    picked, _, _ = subject.join(
        [registry.get(SILENCE)], registry, state, "silence the checkout one"
    )
    assert picked == {"alert_id": "alt-2"}


def test_a_procedure_without_a_subject_joins_nothing(registry: SkillRegistry) -> None:
    state = _shown_alerts(registry)
    assert subject.join([registry.get(HEALTH)], registry, state, "alt-1") == ({}, None, [])


def test_restart_it_after_a_health_read_needs_no_question(example_settings: Settings) -> None:
    provider = Capturing(
        [
            _route("how is search doing", HEALTH),
            _plan(HEALTH, [_op("service.status", {"service": "search"})]),
            {"response": {"reply": "search is running."}},
            _route("restart it", RESTART),
            _plan(
                RESTART,
                [
                    _op("service.status", {"service": "search"}),
                    _op("service.restart", {"service": "search"}, write=True),
                ],
            ),
        ]
    )
    engine = build_engine(example_settings, provider=provider, audit_sink=MemorySink())
    engine.turn("s", "how is search doing")
    result = engine.turn("s", "restart it")
    assert result.pending is not None and result.pending.kind == "approval"
    planned = provider.conversations[-1]
    said = [message.content for message in planned if message.role == "user"]
    assert said[-1] == "restart it"
    focus = [m.content for m in planned if m.content.startswith(prompts.FOCUS_HEADING)]
    assert focus == [f'{prompts.FOCUS_HEADING}: {{"service": "search"}}']
    assert engine.state("s").pending_goal.collected_inputs == {"service": "search"}
    reply = [event for event in result.events if event.kind == "reply"][-1]
    assert reply.data["subject"]["procedures"] == [RESTART]


# Comparatives on the rows that were shown


def test_a_refinement_is_offered_on_what_the_shown_rows_differ_on(
    registry: SkillRegistry,
) -> None:
    state = _shown_alerts(registry)
    offered = subject.refinements(state, registry, ["alert.list"])
    # `service` differs as well, but the read takes any text for it: no enum, no bounds.
    assert offered == [
        {"field": "state", "operation": "alert.list", "values": ["firing", "silenced"]}
    ]


def test_no_refinement_when_the_shown_rows_do_not_differ(registry: SkillRegistry) -> None:
    firing = [alert for alert in ALERTS if alert["state"] == "firing"]
    state = _shown_alerts(registry, firing)
    assert subject.refinements(state, registry, ["alert.list"]) == []


def test_no_refinement_for_a_procedure_that_cannot_read_what_was_shown(
    registry: SkillRegistry,
) -> None:
    state = _shown_alerts(registry)
    assert subject.refinements(state, registry, ["service.status"]) == []


def test_no_refinement_when_one_record_is_in_focus(registry: SkillRegistry) -> None:
    state = _shown_alerts(registry)
    state.focus = {**state.focus, "cardinality": "one", "identifiers": [{"id": "alt-1"}]}
    assert subject.refinements(state, registry, ["alert.list"]) == []


def test_a_numeric_refinement_carries_its_declared_bounds() -> None:
    offered = subject._refinement(
        "replicas", "service.list", {"type": "integer", "minimum": 0, "maximum": 20}, [2, 8]
    )
    assert offered == {
        "field": "replicas",
        "operation": "service.list",
        "shown_minimum": 2,
        "shown_maximum": 8,
        "minimum": 0,
        "maximum": 20,
    }
    assert subject._refinement("name", "x", {"type": "string"}, ["a", "b"]) is None


@pytest.mark.parametrize("values", [["open"], ["open", "closed"]])
def test_an_enum_offers_only_the_shown_values_it_allows(values: list[str]) -> None:
    offered = subject._refinement(
        "state", "incident.list", {"enum": ["open", "mitigated", "closed"]}, values
    )
    assert offered["values"] == values
