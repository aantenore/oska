"""The turn loop: what it stores, what it refuses and the order it runs things in."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from oska.audit.sink import MemorySink
from oska.catalog.validator import CatalogError
from oska.config import Settings
from oska.contracts.common import (
    ConnectorResult,
    Message,
    PendingGoal,
    TurnKind,
)
from oska.contracts.receipt import VerificationStatus, WriteReceipt
from oska.engine import outcomes as build
from oska.engine.engine import Engine
from oska.engine.factory import build_engine
from oska.engine.outcomes import outcome_summary
from oska.engine.phrases import Phrases
from oska.engine.session_store import SessionStore
from oska.engine.state import SessionState
from oska.governance.ledger import WriteLedger
from oska.planner.scripted import ScriptedModelProvider

RESTART_REQUEST = "the search service in staging needs a restart, it is wedged again"
TRIAGE_REQUEST = "run triage on incident inc-501 for the payments service"
CONTAINER_RUNTIME = "integration.container_runtime"
PHRASES = Phrases.load()


def _settings(repo_root: Path, state_dir: Path) -> Settings:
    return Settings(
        skills_roots=[repo_root / "examples" / "ops" / "skills"],
        runtime_config=repo_root / "configs" / "runtime.json",
        model_config_path=repo_root / "configs" / "model.json",
        connectors_config=repo_root / "examples" / "ops" / "configs" / "connectors.json",
        schemas_dir=repo_root / "schemas",
        state_dir=state_dir,
        provider="scripted",
    )


def _engine(repo_root: Path, state_dir: Path, script: list[dict]) -> Engine:
    return build_engine(
        _settings(repo_root, state_dir),
        provider=ScriptedModelProvider(script),
        audit_sink=MemorySink(),
    )


def _route(kind: str, runbook_id: str | None = None) -> dict:
    return {
        "response": {
            "kind": kind,
            "runbook_id": runbook_id,
            "confidence": 0.9,
            "language": "en",
        }
    }


def _log_search_plan(service: str = "payments") -> dict:
    return {
        "response": {
            "runbook_id": "runbook.log_search",
            "operations": [
                {
                    "integration_id": CONTAINER_RUNTIME,
                    "operation": "service.logs",
                    "payload": {"service": service, "pattern": "timeout", "lines": 20},
                    "write": False,
                    "purpose": "read the recent log lines",
                }
            ],
            "missing": [],
            "reply_intent": "report what the logs hold",
        }
    }


def _restart_plan(service: str = "search") -> dict:
    return {
        "response": {
            "runbook_id": "runbook.service_restart",
            "operations": [
                {
                    "integration_id": CONTAINER_RUNTIME,
                    "operation": "service.status",
                    "payload": {"service": service},
                    "write": False,
                    "purpose": "read the current state",
                },
                {
                    "integration_id": CONTAINER_RUNTIME,
                    "operation": "service.restart",
                    "payload": {"service": service},
                    "write": True,
                    "purpose": "restart the service",
                },
            ],
            "missing": [],
            "reply_intent": "report the restart",
        }
    }


class _FailingConnector:
    """Delegates every read and refuses the write, after looking at the ledger."""

    def __init__(self, inner, ledger: WriteLedger, session_id: str) -> None:
        self.id = inner.id
        self._inner = inner
        self._ledger = ledger
        self._session_id = session_id
        self.seen_before_call: list[str] = []

    def execute(self, operation: str, payload: dict) -> ConnectorResult:
        if operation != "service.restart":
            return self._inner.execute(operation, payload)
        key = self._ledger.key(self.id, operation, payload, ["service"])
        held = self._ledger.lookup(self._session_id, key)
        if held is not None:
            self.seen_before_call.append(held.status)
        raise RuntimeError("the runtime refused the call")


def test_session_store_round_trip(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.sqlite")
    assert store.load("fresh").turn_count == 0

    state = SessionState(
        session_id="s1",
        language="it",
        pending_goal=PendingGoal(runbook_ids=["runbook.service_restart"], step_index=0),
        last_entities=["entity.service"],
        history=[Message(role="user", content="ciao")],
        receipts=["r-1"],
        turn_count=3,
    )
    store.save(state)
    loaded = store.load("s1")

    assert loaded.language == "it"
    assert loaded.pending_goal is not None
    assert loaded.pending_goal.runbook_ids == ["runbook.service_restart"]
    assert loaded.receipts == ["r-1"]
    assert loaded.turn_count == 3
    assert store.sessions() == ["s1"]


def test_approve_refuses_a_payload_that_changed(repo_root: Path, tmp_path: Path) -> None:
    engine = _engine(repo_root, tmp_path, [_route("new_task", "runbook.service_restart"),
                                           _restart_plan()])
    session_id = "tampered"
    engine.turn(session_id, RESTART_REQUEST)

    state = engine.state(session_id)
    assert state.pending_approval is not None
    state.pending_approval.exact_payload = {"service": "payments"}
    SessionStore(tmp_path / "sessions.sqlite").save(state)

    result = engine.turn(session_id, "yes")

    assert [outcome.kind for outcome in result.outcomes] == ["rejected"]
    assert engine.receipts(session_id) == []
    assert engine.state(session_id).pending_approval is None


def test_approve_refuses_a_plan_that_moved_under_the_approval(
    repo_root: Path, tmp_path: Path
) -> None:
    """The approval covers one operation of a stored plan; editing the plan voids it."""
    engine = _engine(repo_root, tmp_path, [_route("new_task", "runbook.service_restart"),
                                           _restart_plan()])
    session_id = "moved-plan"
    engine.turn(session_id, RESTART_REQUEST)

    state = engine.state(session_id)
    pending = state.pending_approval
    assert pending is not None
    pending.plan.operations[pending.op_index].payload = {"service": "payments"}
    SessionStore(tmp_path / "sessions.sqlite").save(state)

    result = engine.turn(session_id, "yes")

    assert [outcome.kind for outcome in result.outcomes] == ["rejected"]
    assert result.outcomes[0].reason == "approval payload no longer matches the plan"
    assert engine.receipts(session_id) == []
    assert engine.state(session_id).pending_approval is None


def test_a_write_withdrawn_from_the_catalog_is_not_run_on_an_old_approval(
    repo_root: Path, catalog_copy: Path, tmp_path: Path
) -> None:
    """The approval is given in one turn and the declarations are read again in the next."""
    settings = _settings(repo_root, tmp_path / "state").model_copy(
        update={"skills_roots": [catalog_copy]}
    )
    engine = build_engine(
        settings,
        provider=ScriptedModelProvider(
            [_route("new_task", "runbook.service_restart"), _restart_plan()]
        ),
        audit_sink=MemorySink(),
    )
    session_id = "withdrawn"
    engine.turn(session_id, RESTART_REQUEST)
    assert engine.state(session_id).pending_approval is not None

    _drop_operation(catalog_copy, "container_runtime", "service.restart")

    result = engine.turn(session_id, "yes")

    assert [outcome.kind for outcome in result.outcomes] == ["invalid_plan"]
    assert "service.restart" in (result.outcomes[0].reason or "")
    assert engine.receipts(session_id) == []
    assert engine.state(session_id).pending_approval is None


def _drop_operation(root: Path, integration: str, operation: str) -> None:
    """Rewrite an integration skill of a catalog root without one of its operations."""
    path = root / "integration" / integration / "v1" / "skill.md"
    _, header, body = path.read_text(encoding="utf-8").split("---\n", 2)
    frontmatter = yaml.safe_load(header)
    frontmatter["operations"] = [
        spec for spec in frontmatter["operations"] if spec["name"] != operation
    ]
    rendered = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True)
    path.write_text(f"---\n{rendered}---\n{body}", encoding="utf-8")


def test_write_intent_is_recorded_before_the_call(repo_root: Path, tmp_path: Path) -> None:
    engine = _engine(repo_root, tmp_path, [_route("new_task", "runbook.service_restart"),
                                           _restart_plan()])
    session_id = "intent"
    engine.turn(session_id, RESTART_REQUEST)
    connector = _FailingConnector(
        engine.connectors.get(CONTAINER_RUNTIME),
        WriteLedger(tmp_path / "ledger.sqlite"),
        session_id,
    )
    engine.connectors.register(CONTAINER_RUNTIME, connector)

    result = engine.turn(session_id, "yes")

    assert connector.seen_before_call == ["pending"]
    receipts = engine.receipts(session_id)
    assert len(receipts) == 1
    assert receipts[0].status == "error"
    assert str(receipts[0].verification) == "unverified"
    assert [outcome.kind for outcome in result.outcomes] == ["receipt"]


def test_a_turn_while_an_approval_waits_is_never_read_as_an_answer(
    repo_root: Path, tmp_path: Path
) -> None:
    """A free-text turn cancels the approval and starts again, whatever it is classified as."""
    engine = _engine(
        repo_root,
        tmp_path,
        [
            _route("new_task", "runbook.service_restart"),
            _restart_plan(),
            _route("provide_input", "runbook.log_search"),
            _log_search_plan(),
        ],
    )
    session_id = "not-an-answer"
    engine.turn(session_id, RESTART_REQUEST)
    approval = engine.state(session_id).pending_approval
    assert approval is not None

    result = engine.turn(session_id, "search the payments logs for timeouts instead")

    assert [outcome.kind for outcome in result.outcomes] == ["cancelled", "read"]
    assert result.outcomes[0].operation == "service.restart"
    assert result.outcomes[1].operation == "service.logs"
    assert engine.receipts(session_id) == []
    assert engine.state(session_id).pending_approval is None


def test_an_approval_belongs_to_the_session_that_asked_for_it(
    repo_root: Path, tmp_path: Path
) -> None:
    engine = _engine(
        repo_root,
        tmp_path,
        [
            _route("new_task", "runbook.service_restart"),
            _restart_plan(),
            _route("off_scope"),
        ],
    )
    engine.turn("session-a", RESTART_REQUEST)

    elsewhere = engine.turn("session-b", "yes")

    assert elsewhere.outcomes == []
    assert engine.receipts("session-a") == []
    assert engine.receipts("session-b") == []
    assert engine.state("session-a").pending_approval is not None
    assert engine.state("session-b").pending_approval is None

    result = engine.turn("session-a", "yes")

    assert [outcome.kind for outcome in result.outcomes] == ["receipt"]
    assert len(engine.receipts("session-a")) == 1
    assert engine.receipts("session-b") == []


def test_a_catalog_that_carries_an_error_is_never_served(
    repo_root: Path,
    tmp_path: Path,
    write_skill: Callable[..., Path],
    runbook_frontmatter: Callable[..., dict],
) -> None:
    write_skill(runbook_frontmatter(integrations=["integration.absent"]))
    settings = _settings(repo_root, tmp_path / "state").model_copy(
        update={"skills_roots": [tmp_path / "skills"]}
    )

    with pytest.raises(CatalogError) as raised:
        build_engine(settings, provider=ScriptedModelProvider([]), audit_sink=MemorySink())

    assert {issue.code for issue in raised.value.issues} == {"unknown_reference"}


def test_a_composite_expands_into_its_runbooks_in_order(
    repo_root: Path, tmp_path: Path
) -> None:
    engine = _engine(
        repo_root,
        tmp_path,
        [
            _route("new_task", "composite.incident_triage"),
            {
                "response": {
                    "runbook_id": "runbook.service_health",
                    "operations": [],
                    "missing": [{"field": "service", "question": "Which service?"}],
                    "reply_intent": "ask which service",
                }
            },
        ],
    )
    session_id = "composite"

    result = engine.turn(session_id, TRIAGE_REQUEST)

    assert result.pending is not None
    assert result.pending.kind == "input"
    goal = engine.state(session_id).pending_goal
    assert goal is not None
    assert goal.runbook_ids == [
        "runbook.service_health",
        "runbook.log_search",
        "runbook.incident_note",
    ]
    assert goal.step_index == 0


@pytest.mark.parametrize("kind", [TurnKind.greeting, TurnKind.off_scope])
def test_catalog_replies_need_no_model(repo_root: Path, tmp_path: Path, kind: TurnKind) -> None:
    engine = _engine(repo_root, tmp_path, [_route(kind.value)])

    result = engine.turn("scope", "anything at all")

    assert result.outcomes == []
    assert engine.resolver.declared_scope().rstrip(".") in result.reply


def test_without_a_provider_the_engine_still_answers_for_itself(
    repo_root: Path, tmp_path: Path
) -> None:
    engine = build_engine(_settings(repo_root, tmp_path), audit_sink=MemorySink())

    greeting = engine.turn("no-model", "hello")
    request = engine.turn("no-model", RESTART_REQUEST)

    assert engine.resolver.declared_scope().rstrip(".") in greeting.reply
    assert "model provider" in request.reply
    assert request.outcomes == []
    assert engine.receipts("no-model") == []


def test_the_summary_never_calls_an_unconfirmed_write_verified() -> None:
    receipt = WriteReceipt(
        receipt_id="r-7",
        session_id="s",
        ledger_key="k",
        integration_id=CONTAINER_RUNTIME,
        operation="service.restart",
        payload={"service": "search"},
        status="executed",
        verification=VerificationStatus.failed,
        verification_evidence={"comparisons": {"service.status": {"match": False}}},
        created_at=datetime.now(UTC),
    )
    lines = outcome_summary(
        [
            build.receipt_outcome(receipt),
            build.ledger_hit_outcome(receipt),
            build.invalid_plan_outcome("runbook.service_restart", ["no such operation"]),
        ],
        PHRASES,
        "en",
    )

    assert lines[0] == (
        "service.restart ran, but the read-back contradicted it. "
        "Receipt r-7 is not verified."
    )
    assert "already done" not in lines[1]
    assert "not confirmed: receipt r-7" in lines[1]
    assert "It is not sent again." in lines[1]
    assert "no such operation" in lines[2]


def test_a_confirmed_write_found_in_the_ledger_is_reported_as_done() -> None:
    """Only a receipt that is executed and verified may be called done."""
    receipt = WriteReceipt(
        receipt_id="r-8",
        session_id="s",
        ledger_key="k",
        integration_id="integration.container_runtime",
        operation="service.restart",
        payload={"service": "search"},
        status="executed",
        verification=VerificationStatus.verified,
        created_at=datetime.now(UTC),
    )
    lines = outcome_summary([build.ledger_hit_outcome(receipt)], PHRASES, "en")
    assert lines == ["service.restart was already done in this session, receipt r-8."]


# What the reply writer may say about a write


def _written(text: str, fact: str) -> dict:
    """A written reply, asserting the brief told the writer whether the write landed."""
    return {"expect_contains": fact, "response": {"reply": text}}


class _Briefs(ScriptedModelProvider):
    """A scripted provider that keeps the last user message of every call it answers."""

    def __init__(self, script: list[dict]) -> None:
        super().__init__(script)
        self.briefs: list[str] = []

    def complete_json(
        self, system: str, messages: list[Message], schema: dict, *, temperature: float = 0.0
    ) -> dict:
        said = [message.content for message in messages if message.role == "user"]
        self.briefs.append(said[-1] if said else "")
        return super().complete_json(system, messages, schema, temperature=temperature)


def test_a_written_reply_is_set_aside_when_a_write_did_not_land(
    repo_root: Path, tmp_path: Path
) -> None:
    """The writer claims success; the read-back contradicted the write, so the template speaks."""
    claim = "Done: reporting was restarted and is running again."
    provider = _Briefs(
        [
            _route("new_task", "runbook.service_restart"),
            _restart_plan("reporting"),
            {"response": {"reply": claim}},
        ]
    )
    engine = build_engine(
        _settings(repo_root, tmp_path), provider=provider, audit_sink=MemorySink()
    )
    engine.turn("s", "reporting in staging is down again, restart it")
    stopped = {
        "name": "reporting",
        "environment": "staging",
        "status": "stopped",
        "version": "4.0.0",
        "replicas": 1,
        "restarts": 7,
    }
    engine.connectors.get(CONTAINER_RUNTIME).set_behavior(
        "service.status", {"override": {"service": stopped}}
    )

    result = engine.turn("s", "yes")

    assert [outcome.kind for outcome in result.outcomes] == ["receipt"]
    assert provider.remaining() == 0  # the reply call was made
    assert claim not in result.reply
    assert "the read-back contradicted it" in result.reply
    assert "did not land or is not confirmed. Do not say it was done." in provider.briefs[-1]


def test_a_written_reply_is_kept_when_every_write_landed(
    repo_root: Path, tmp_path: Path
) -> None:
    claim = "Done: search was restarted and is running again."
    engine = _engine(
        repo_root,
        tmp_path,
        [
            _route("new_task", "runbook.service_restart"),
            _restart_plan("search"),
            _written(claim, "landed, confirmed by its read-back."),
        ],
    )
    engine.turn("s", RESTART_REQUEST)

    result = engine.turn("s", "yes")

    assert result.reply.startswith(claim)
    assert "Receipt" in result.reply  # the deterministic receipt line still follows


# The policy at approval judges only the state it could read again


def test_an_approval_whose_state_cannot_be_read_again_sends_nothing(
    repo_root: Path, tmp_path: Path
) -> None:
    """The production rule is conditioned on the environment the status read reports.

    Two approvals stand. When the status read before the write does not come
    back, the policy would judge the restart without the environment it is
    conditioned on; the write is refused as unavailable instead, and nothing
    is sent.
    """
    engine = _engine(
        repo_root,
        tmp_path,
        [_route("new_task", "runbook.service_restart"), _restart_plan("checkout")],
    )
    engine.turn("prod", "the checkout service in production needs a restart")
    engine.turn("prod", "yes")
    engine.connectors.get(CONTAINER_RUNTIME).set_behavior(
        "service.status", {"unreachable": True, "error": "the runtime did not answer"}
    )

    result = engine.turn("prod", "yes")

    assert [outcome.kind for outcome in result.outcomes] == ["unavailable"]
    assert result.outcomes[0].operation == "service.restart"
    assert "could not be confirmed" in result.reply
    assert "Nothing was sent" in result.reply
    assert engine.receipts("prod") == []
    assert not [
        event
        for event in result.events
        if event.kind == "execute" and event.data.get("operation") == "service.restart"
    ]
    assert engine.state("prod").pending_approval is None


def test_a_turn_routed_without_the_model_learns_the_language_from_the_plan(
    example_settings,  # type: ignore[no-untyped-def]
) -> None:
    """The planner already runs; the language it reports is kept, at no extra call."""
    from oska.audit.sink import MemorySink
    from oska.engine.factory import build_engine
    from oska.planner.scripted import ScriptedModelProvider

    script = [
        {
            "purpose": "plan",
            "response": {
                "runbook_id": "runbook.service_health",
                "operations": [
                    {
                        "integration_id": "integration.container_runtime",
                        "operation": "service.status",
                        "payload": {"service": "search"},
                        "write": False,
                        "purpose": "read the service",
                    }
                ],
                "missing": [],
                "reply_intent": "report the state",
                "language": "it",
            },
        },
        {"purpose": "reply", "response": {"reply": "Il servizio search funziona."}},
    ]
    engine = build_engine(
        example_settings, provider=ScriptedModelProvider(script), audit_sink=MemorySink()
    )
    engine.turn("lang", "come sta il servizio search?")
    assert engine.state("lang").language == "it"


def test_a_model_that_cannot_read_the_answer_leaves_the_approval_waiting(
    example_settings: Settings, repo_root: Path
) -> None:
    """A turn the model could not classify is never taken as a decision on a held write."""
    scenario = json.loads(
        (repo_root / "examples" / "ops" / "scenarios" / "02_restart_staging.json").read_text(
            encoding="utf-8"
        )
    )
    # The router and the plan of the first turn only: the next call finds no answer.
    script = scenario["provider_script"][:2]
    engine = build_engine(
        example_settings, provider=ScriptedModelProvider(script), audit_sink=MemorySink()
    )

    asked = engine.turn("held", scenario["turns"][0]["user"])
    assert asked.pending is not None and asked.pending.approval is not None
    approval_id = asked.pending.approval.approval_id

    unread = engine.turn("held", "go on then, do it please")

    assert unread.pending is not None and unread.pending.approval is not None
    assert unread.pending.approval.approval_id == approval_id
    assert engine.state("held").pending_approval is not None
    assert not [event for event in unread.events if event.kind == "approval_decided"]
    route = next(event for event in unread.events if event.kind == "route")
    assert route.data["reason"] == "the model could not classify the turn"
    assert "yes" in unread.reply

    approved = engine.turn("held", "yes")
    assert [outcome.kind for outcome in approved.outcomes if outcome.kind == "receipt"]
