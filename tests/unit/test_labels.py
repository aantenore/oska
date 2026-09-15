"""What a turn offers, in the language it answered in, for no extra model call.

The wording of an offer is declared by the author of a procedure, in whatever
language the catalog is written in. The reply is written in the language the
person used. The two met badly: a turn could answer in Italian and then offer
its next steps in English.

The fix costs nothing, and these tests are what says so. The offer travels in
the call that writes the reply, as a key and a label, and comes back with the
label rewritten. One turn, one reply call, whether or not it offers anything;
an offer nothing came back for keeps the words its author declared; and a label
under a key the turn never offered is dropped rather than added.

The last test here is about a different line entirely. A preference about how
an answer reads reaches the reply writer and nothing else. It is checked the
only way that means anything: by running the turn and reading every prompt the
model was handed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from opsdeck.audit.sink import MemorySink
from opsdeck.catalog.registry import SkillRegistry
from opsdeck.catalog.resolver import (
    CapabilityResolver,
    SuggestedAction,
    offer,
    relabel,
)
from opsdeck.config import ReplyConfig, Settings
from opsdeck.contracts.common import Message, Outcome
from opsdeck.engine.factory import build_engine
from opsdeck.engine.reply import ReplyBuilder, offered_lines
from opsdeck.engine.state import SessionState
from opsdeck.improvement.preferences import Preference, preferences_root, write_preferences
from opsdeck.planner.planner import Planner
from opsdeck.planner.scripted import ScriptedModelProvider

CONTAINER_RUNTIME = "integration.container_runtime"
RESTART = "runbook.service_restart"
ROLLBACK = "runbook.deployment_rollback"
HEALTH = "runbook.service_health"

HEALTH_REQUEST = "come procede il servizio di ricerca in staging"
LOGS_REQUEST = "cerca errori nei log del servizio di ricerca"


class Counter:
    """A provider that replays a script and counts what it was asked for."""

    name = "counter"

    def __init__(self, script: list[dict]) -> None:
        self._inner = ScriptedModelProvider(script)
        self.completions = 0
        self.exchanges: list[str] = []
        self.systems: list[str] = []

    def complete_json(
        self,
        system: str,
        messages: list[Message],
        schema: dict,
        *,
        temperature: float = 0.0,
    ) -> dict:
        self.completions += 1
        self.systems.append(system)
        self.exchanges.append("\n".join([system, *(message.content for message in messages)]))
        return self._inner.complete_json(system, messages, schema, temperature=temperature)

    def embed(self, texts: list[str]) -> list[list[float]]:
        return self._inner.embed(texts)


def _route(runbook_id: str, language: str = "it") -> dict:
    return {
        "purpose": "route",
        "response": {
            "kind": "new_task",
            "runbook_id": runbook_id,
            "confidence": 0.92,
            "language": language,
        },
    }


def _plan(runbook_id: str, operations: list[dict], intent: str) -> dict:
    return {
        "purpose": "plan",
        "response": {
            "runbook_id": runbook_id,
            "operations": operations,
            "missing": [],
            "reply_intent": intent,
        },
    }


def _read(operation: str, payload: dict) -> dict:
    return {
        "integration_id": CONTAINER_RUNTIME,
        "operation": operation,
        "payload": payload,
        "write": False,
        "purpose": "read what was asked for",
    }


def _health_script(reply: dict, language: str = "it") -> list[dict]:
    """Route, plan and reply for a health check, which leaves something on offer."""
    return [
        _route("runbook.service_health", language),
        _plan(
            "runbook.service_health",
            [_read("service.status", {"service": "search"})],
            "report the state of search",
        ),
        {"purpose": "reply", "response": reply},
    ]


def _logs_script(reply: dict) -> list[dict]:
    """The same three calls for a log search, which touches no entity record."""
    return [
        _route("runbook.log_search"),
        _plan(
            "runbook.log_search",
            [_read("service.logs", {"service": "search", "pattern": "error", "lines": 20})],
            "report the matching log lines",
        ),
        {"purpose": "reply", "response": reply},
    ]


def _engine(settings: Settings, provider: Counter):
    return build_engine(settings, provider=provider, audit_sink=MemorySink())


# --- the offer, in the language of the turn --------------------------------


def test_the_offer_comes_back_in_the_language_of_the_turn(
    example_settings: Settings,
) -> None:
    """The labels are handed over and returned by the one call that writes the reply."""
    provider = Counter(
        _health_script(
            {
                "reply": "Il servizio search e in esecuzione.",
                "action_labels": {
                    RESTART: "riavvialo",
                    ROLLBACK: "riportalo a una versione precedente",
                },
            }
        )
    )

    result = _engine(example_settings, provider).turn("labels", HEALTH_REQUEST)

    lines = result.reply.splitlines()
    assert lines[0] == "Il servizio search e in esecuzione."
    assert "- riavvialo" in lines
    assert "- riportalo a una versione precedente" in lines
    assert "restart it" not in result.reply


def test_an_action_no_label_came_back_for_keeps_its_declared_wording(
    example_settings: Settings,
) -> None:
    reply = {"reply": "Il servizio e in esecuzione.", "action_labels": {RESTART: "riavvialo"}}
    provider = Counter(_health_script(reply))

    result = _engine(example_settings, provider).turn("partial", HEALTH_REQUEST)

    assert "- riavvialo" in result.reply
    assert "- roll it back" in result.reply


def test_the_offer_the_catalog_declares_is_used_when_nothing_comes_back(
    example_settings: Settings,
) -> None:
    provider = Counter(_health_script({"reply": "Search is running."}))

    result = _engine(example_settings, provider).turn("declared", HEALTH_REQUEST)

    assert result.reply.splitlines()[0] == "Search is running."
    assert "- restart it" in result.reply


# --- the rule that nothing may add a call ----------------------------------


def test_a_turn_that_offers_actions_costs_the_calls_of_one_that_offers_none(
    repo_root: Path, tmp_path: Path
) -> None:
    """Translating an offer travels with the reply; it is never a call of its own."""
    offering = Counter(
        _health_script(
            {"reply": "Il servizio e in esecuzione.", "action_labels": {RESTART: "riavvialo"}}
        )
    )
    silent = Counter(_logs_script({"reply": "Nessuna riga corrisponde."}))

    with_offer = _engine(_settings(repo_root, tmp_path / "a"), offering).turn(
        "offering", HEALTH_REQUEST
    )
    without = _engine(_settings(repo_root, tmp_path / "b"), silent).turn("silent", LOGS_REQUEST)

    assert "- riavvialo" in with_offer.reply
    assert offered_lines([]) == []
    assert not [line for line in without.reply.splitlines() if line.startswith("- ")]
    assert offering.completions == silent.completions == 3


# --- the keys ---------------------------------------------------------------


def test_a_label_under_a_key_the_turn_did_not_offer_is_dropped() -> None:
    actions = [SuggestedAction(key=RESTART, label="restart it")]

    relabel(actions, {RESTART: "riavvialo", "runbook.invented": "fai qualcosa"})

    assert [(action.key, action.label) for action in actions] == [(RESTART, "riavvialo")]


def test_an_empty_label_leaves_the_declared_wording_alone() -> None:
    actions = [SuggestedAction(key=RESTART, label="restart it")]

    relabel(actions, {RESTART: "   "})

    assert actions[0].label == "restart it"


def test_two_actions_never_share_a_key() -> None:
    actions: list[SuggestedAction] = []

    offer(actions, RESTART, "restart it")
    offer(actions, RESTART, "restart it")
    offer(actions, RESTART, "restart it (the next step this service needs)")

    assert [action.key for action in actions] == [RESTART, f"{RESTART}#2"]
    assert len(actions) == 2


def test_an_entity_with_a_lifecycle_keys_its_offers_by_procedure(
    registry: SkillRegistry, resolver: CapabilityResolver
) -> None:
    offered = resolver.offered_actions_for_entity("entity.alert", "firing")

    assert offered is not None
    assert [(action.key, action.label) for action in offered] == [
        ("runbook.alert_silence", "silence it")
    ]
    assert resolver.suggestions_for_entity("entity.alert", "firing") == ["silence it"]


def test_the_deterministic_reply_lists_what_it_offers(
    resolver: CapabilityResolver, tmp_path: Path
) -> None:
    """With no provider the template writes the reply and the offer is still listed."""
    builder = ReplyBuilder(None, resolver, ReplyConfig(), tmp_path / "preferences")

    reply = builder.build(
        "outcomes",
        SessionState(session_id="s1"),
        [Outcome(kind="read", integration_id=CONTAINER_RUNTIME, operation="service.status")],
        actions=[SuggestedAction(key=RESTART, label="restart it")],
    )

    assert reply.splitlines()[-1] == "- restart it"


# --- the prompts a routing decision is built from ---------------------------


def test_the_routing_prompts_keep_the_wording_the_catalog_declares(
    example_settings: Settings, registry: SkillRegistry
) -> None:
    """The reply is written in the language of the turn; the routing is not.

    The same request is answered twice, once as a turn in Italian and once as a
    turn in English. The prompt the router reads and the prompt the planner
    reads are the same text both times, and both hold the wording the catalog
    declares; only the prompt that writes the reply names a language at all.
    """
    in_italian = Counter(_health_script({"reply": "Il servizio e in esecuzione."}))
    in_english = Counter(_health_script({"reply": "Search is running."}, language="en"))

    _engine(example_settings, in_italian).turn("answered-in-italian", HEALTH_REQUEST)
    _engine(example_settings, in_english).turn("answered-in-english", HEALTH_REQUEST)

    routing, planning, reply = in_italian.systems
    other_routing, other_planning, other_reply = in_english.systems
    declared = registry.get(HEALTH).frontmatter.description

    assert routing.startswith("Classify one message")
    assert planning.startswith("You plan the operations")
    assert reply.startswith("You write the reply")
    assert declared in routing
    assert declared in planning
    assert routing == other_routing
    assert planning == other_planning
    assert "language" not in routing.lower()
    assert "language" not in planning.lower()
    assert "in this language: it" in reply
    assert "in this language: en" in other_reply


# --- a preference reads, and never acts -------------------------------------


def test_a_preference_reaches_the_reply_writer_and_no_other_prompt(
    example_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It is a fact for the writer of the reply. The planner never sees it."""
    preference = "keep the answer to two sentences"
    _installation(monkeypatch, example_settings)
    write_preferences(
        preferences_root(Path(example_settings.state_dir)),
        "reads-short",
        [Preference(text=preference, session_id="reads-short", turn=1)],
    )
    provider = Counter(_health_script({"reply": "Il servizio e in esecuzione."}))

    _engine(example_settings, provider).turn("reads-short", HEALTH_REQUEST)

    carrying = [held for held in provider.exchanges if preference in held]
    assert len(carrying) == 1
    assert "reply_intent" in carrying[0]
    assert provider.exchanges.index(carrying[0]) == len(provider.exchanges) - 1


def test_a_preference_of_another_session_is_not_read(
    example_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _installation(monkeypatch, example_settings)
    write_preferences(
        preferences_root(Path(example_settings.state_dir)),
        "someone-else",
        [Preference(text="always greet me", session_id="someone-else", turn=1)],
    )
    provider = Counter(_health_script({"reply": "Il servizio e in esecuzione."}))

    _engine(example_settings, provider).turn("not-them", HEALTH_REQUEST)

    assert not [held for held in provider.exchanges if "always greet me" in held]


def test_a_preference_file_that_cannot_be_read_is_no_preference(
    resolver: CapabilityResolver, tmp_path: Path
) -> None:
    root = tmp_path / "preferences"
    root.mkdir()
    (root / "broken.json").write_text("{not json", encoding="utf-8")
    builder = ReplyBuilder(None, resolver, ReplyConfig(), root)

    assert builder.preferences("broken") == []
    assert builder.preferences("never-heard-of") == []


def _installation(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    """Name this state directory in the environment, as a deployment does.

    The reply writer is built with the reply configuration alone, so where it
    reads preferences from is the state directory of the installation the
    environment names.
    """
    monkeypatch.setenv("OPSDECK_STATE_DIR", str(settings.state_dir))


def _settings(repo_root: Path, state_dir: Path) -> Settings:
    """Settings over the example domain, with state under the test."""
    return Settings(
        skills_roots=[repo_root / "examples" / "ops" / "skills"],
        runtime_config=repo_root / "configs" / "runtime.json",
        model_config_path=repo_root / "configs" / "model.json",
        connectors_config=repo_root / "examples" / "ops" / "configs" / "connectors.json",
        schemas_dir=repo_root / "schemas",
        state_dir=state_dir,
        provider="scripted",
    )


@pytest.mark.parametrize("payload", [{}, {"actions": []}])
def test_the_reply_schema_accepts_an_answer_without_labels(
    registry: SkillRegistry, resolver: CapabilityResolver, payload: dict
) -> None:
    """A reply writer that returns only prose is answering correctly."""
    provider = ScriptedModelProvider([{"purpose": "reply", "response": {"reply": "Done."}}])
    planner = Planner(provider, registry, resolver)
    actions = [SuggestedAction(key=RESTART, label="restart it")]

    written = planner.write_reply([], json.dumps(payload), actions, "en")

    assert written == "Done."
    assert actions[0].label == "restart it"
