"""Planner, scripted provider and the chat-completions provider."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from oska.catalog.registry import SkillRegistry
from oska.catalog.resolver import CapabilityResolver, SuggestedAction
from oska.contracts.common import Hydrated, Message, Outcome, RankedSkill
from oska.contracts.plan import Plan
from oska.planner.openai_compatible import OpenAICompatibleProvider
from oska.planner.planner import InvalidPlanError, Planner, validate_plan
from oska.planner.provider import ModelProviderError
from oska.planner.scripted import ScriptedModelProvider, ScriptMismatchError

RUNBOOK = "runbook.service_restart"
INTEGRATION = "integration.container_runtime"
CONVERSATION = [Message(role="user", content="restart the checkout service")]
_FENCED_ANSWER = 'Here you go:\n```json\n{"reply": "done"}\n```\nthanks'


@pytest.fixture
def hydrated(registry: SkillRegistry) -> Hydrated:
    """The restart procedure with everything it declares."""
    return registry.closure(RUNBOOK)


def _plan_response(operations: list[dict], missing: list[dict] | None = None) -> dict:
    return {
        "runbook_id": RUNBOOK,
        "operations": operations,
        "missing": missing or [],
        "reply_intent": "report the restart",
    }


def _operation(name: str, payload: dict, write: bool) -> dict:
    return {
        "integration_id": INTEGRATION,
        "operation": name,
        "payload": payload,
        "write": write,
        "purpose": "act on the service",
    }


def _planner(
    script: list[dict], registry: SkillRegistry, resolver: CapabilityResolver
) -> tuple[Planner, ScriptedModelProvider]:
    provider = ScriptedModelProvider(script)
    return Planner(provider, registry, resolver), provider


class _Recorder:
    """A provider that keeps every exchange it was asked about, prompt included."""

    name = "recorder"

    def __init__(self, inner: ScriptedModelProvider) -> None:
        self._inner = inner
        self._seen: list[str] = []

    def seen(self) -> list[str]:
        """Every system prompt and message this provider was handed, one string each."""
        return list(self._seen)

    def complete_json(
        self,
        system: str,
        messages: list[Message],
        schema: dict,
        *,
        temperature: float = 0.0,
    ) -> dict:
        self._seen.append("\n".join([system, *(message.content for message in messages)]))
        return self._inner.complete_json(system, messages, schema, temperature=temperature)

    def embed(self, texts: list[str]) -> list[list[float]]:
        return self._inner.embed(texts)


def test_undeclared_operation_is_rejected_then_repaired(
    hydrated: Hydrated, registry: SkillRegistry, resolver: CapabilityResolver
) -> None:
    script = [
        {
            "purpose": "first attempt, invents an operation",
            "response": _plan_response(
                [_operation("service.delete", {"service": "checkout"}, True)]
            ),
        },
        {
            "purpose": "repair",
            "expect_contains": "declares no operation service.delete",
            "response": _plan_response(
                [_operation("service.status", {"service": "checkout"}, False)]
            ),
        },
    ]
    planner, provider = _planner(script, registry, resolver)

    plan = planner.plan(hydrated, CONVERSATION, {})

    assert [operation.operation for operation in plan.operations] == ["service.status"]
    assert plan.runbook_id == RUNBOOK
    assert provider.remaining() == 0


def test_schema_failure_is_repaired_once_then_raises(
    hydrated: Hydrated, registry: SkillRegistry, resolver: CapabilityResolver
) -> None:
    broken = _plan_response([_operation("service.restart", {"service": 7}, True)])
    script = [
        {"purpose": "first attempt", "response": broken},
        {"purpose": "repair", "expect_contains": "input schema", "response": broken},
    ]
    planner, provider = _planner(script, registry, resolver)

    with pytest.raises(InvalidPlanError) as raised:
        planner.plan(hydrated, CONVERSATION, {})

    assert provider.remaining() == 0
    assert any("input schema" in issue for issue in raised.value.issues)


def test_write_flag_must_match_the_declaration(hydrated: Hydrated) -> None:
    wrong_flag = _operation("service.restart", {"service": "checkout"}, False)
    plan = _plan_from(_plan_response([wrong_flag]))

    issues = validate_plan(plan, hydrated)

    assert any("write=False" in issue for issue in issues)


def test_missing_required_input_is_accepted_when_it_is_asked_for(hydrated: Hydrated) -> None:
    asked = _plan_from(
        _plan_response(
            [_operation("service.restart", {}, True)],
            [{"field": "service", "question": "Which service?"}],
        )
    )
    silent = _plan_from(_plan_response([_operation("service.restart", {}, True)]))

    assert validate_plan(asked, hydrated) == []
    assert any("`missing`" in issue for issue in validate_plan(silent, hydrated))


def test_pick_falls_back_to_the_first_candidate_on_an_invalid_answer(
    registry: SkillRegistry, resolver: CapabilityResolver
) -> None:
    script = [{"response": {"skill_id": "runbook.not_a_procedure"}}]
    planner, _ = _planner(script, registry, resolver)
    candidates = [
        RankedSkill(skill_id=RUNBOOK, score=0.7),
        RankedSkill(skill_id="runbook.service_health", score=0.69),
    ]

    assert planner.pick("restart it", candidates, registry) == RUNBOOK


def test_pick_returns_a_listed_candidate(
    registry: SkillRegistry, resolver: CapabilityResolver
) -> None:
    script = [{"response": {"skill_id": "runbook.service_health"}}]
    planner, _ = _planner(script, registry, resolver)
    candidates = [
        RankedSkill(skill_id=RUNBOOK, score=0.7),
        RankedSkill(skill_id="runbook.service_health", score=0.69),
    ]

    assert planner.pick("how is it doing", candidates, registry) == "runbook.service_health"


def test_write_reply_sees_only_the_outcomes(
    registry: SkillRegistry, resolver: CapabilityResolver
) -> None:
    script = [
        {
            "purpose": "reply",
            "expect_contains": "checkout",
            "response": {"reply": "The service is running."},
        }
    ]
    planner, _ = _planner(script, registry, resolver)
    outcomes = [
        Outcome(
            kind="read",
            integration_id=INTEGRATION,
            operation="service.status",
            data={"service": "checkout"},
        )
    ]

    actions = [SuggestedAction(key=RUNBOOK, label="restart it")]
    reply = planner.write_reply(outcomes, "report the status", actions, "en", CONVERSATION)

    assert reply == "The service is running."
    assert actions[0].label == "restart it"


def test_the_reply_writer_is_handed_the_actions_and_returns_their_wording(
    registry: SkillRegistry, resolver: CapabilityResolver
) -> None:
    """The offer travels as a key and a label, and comes back in the reply's language."""
    script = [
        {
            "purpose": "reply",
            "expect_contains": '"actions": [{"key": "runbook.service_restart"',
            "response": {
                "reply": "Il servizio e in esecuzione.",
                "action_labels": {RUNBOOK: "riavvialo", "runbook.nothing_offered": "ignorami"},
            },
        }
    ]
    planner, provider = _planner(script, registry, resolver)
    actions = [
        SuggestedAction(key=RUNBOOK, label="restart it"),
        SuggestedAction(key="runbook.service_rollback", label="roll it back"),
    ]

    reply = planner.write_reply([], "report the status", actions, "it", CONVERSATION)

    assert reply == "Il servizio e in esecuzione."
    assert [(action.key, action.label) for action in actions] == [
        (RUNBOOK, "riavvialo"),
        ("runbook.service_rollback", "roll it back"),
    ]
    assert provider.consumed() == 1


def test_a_presentation_preference_reaches_the_reply_and_never_the_plan(
    hydrated: Hydrated, registry: SkillRegistry, resolver: CapabilityResolver
) -> None:
    """A preference is a fact for the reply writer and is absent from every plan prompt."""
    preference = "keep the answer to two sentences"
    script = [
        {
            "purpose": "plan",
            "response": _plan_response([_operation("service.status", {"service": "x"}, False)]),
        },
        {"purpose": "reply", "response": {"reply": "Done."}},
    ]
    recorder = _Recorder(ScriptedModelProvider(script))
    planner = Planner(recorder, registry, resolver)

    planner.plan(hydrated, list(CONVERSATION))
    planned = recorder.seen()
    planner.write_reply([], "report it", [], "en", CONVERSATION, [preference])

    assert not any(preference in exchange for exchange in planned)
    assert any(preference in exchange for exchange in recorder.seen()[len(planned) :])


def test_script_mismatch_names_the_expectation_and_the_message() -> None:
    provider = ScriptedModelProvider(
        [{"purpose": "plan", "expect_contains": "rollback", "response": {}}]
    )

    with pytest.raises(ScriptMismatchError) as raised:
        provider.complete_json("system", [Message(role="user", content="restart checkout")], {})

    assert not isinstance(raised.value, ModelProviderError)
    message = str(raised.value)
    assert "rollback" in message
    assert "restart checkout" in message
    assert provider.remaining() == 1


def test_script_exhaustion_names_the_last_purpose() -> None:
    provider = ScriptedModelProvider([{"purpose": "the only plan", "response": {"ok": True}}])
    messages = [Message(role="user", content="restart checkout")]
    provider.complete_json("system", messages, {})

    with pytest.raises(ModelProviderError) as raised:
        provider.complete_json("system", messages, {})

    assert "the only plan" in str(raised.value)
    assert provider.remaining() == 0


def test_a_script_can_be_read_from_a_file(tmp_path: Path) -> None:
    path = tmp_path / "scenario.json"
    path.write_text(
        json.dumps({"name": "a scenario", "provider_script": [{"response": {"reply": "hello"}}]}),
        encoding="utf-8",
    )
    provider = ScriptedModelProvider(path)

    assert provider.remaining() == 1
    assert provider.complete_json("system", CONVERSATION, {}) == {"reply": "hello"}


def test_scripted_embeddings_are_deterministic_and_track_overlap() -> None:
    provider = ScriptedModelProvider([])
    texts = [
        "restart the checkout service",
        "restart the checkout service now",
        "silence an alert for thirty minutes",
    ]

    first = provider.embed(texts)
    second = provider.embed(texts)

    assert first == second
    assert len({len(vector) for vector in first}) == 1
    near = _cosine(first[0], first[1])
    far = _cosine(first[0], first[2])
    assert near > far
    assert far == pytest.approx(0.0, abs=1e-9)


def test_provider_uses_the_json_schema_response_format() -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps({"reply": "done"})}}]},
        )

    provider = _http_provider(handler)

    answer = provider.complete_json("system", CONVERSATION, {"type": "object"})

    assert answer == {"reply": "done"}
    assert seen[0]["response_format"]["type"] == "json_schema"
    assert seen[0]["messages"][0] == {"role": "system", "content": "system"}


def test_provider_falls_back_when_the_endpoint_rejects_response_format() -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        if "response_format" in body:
            return httpx.Response(
                400, json={"error": {"message": "response_format is not supported"}}
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": _FENCED_ANSWER}}
                ]
            },
        )

    provider = _http_provider(handler)

    answer = provider.complete_json("system", CONVERSATION, {"type": "object"})

    assert answer == {"reply": "done"}
    assert len(seen) == 3
    assert seen[1]["response_format"] == {"type": "json_object"}
    assert "JSON schema" in seen[1]["messages"][0]["content"]
    assert "response_format" not in seen[2]
    assert "JSON schema" in seen[2]["messages"][0]["content"]


def test_provider_reports_the_upstream_message() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "invalid key"}})

    provider = _http_provider(handler)

    with pytest.raises(ModelProviderError, match="invalid key"):
        provider.complete_json("system", CONVERSATION, {"type": "object"})


def test_provider_embeds_through_the_embeddings_endpoint() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [0.0, 1.0]},
                    {"index": 0, "embedding": [1.0, 0.0]},
                ]
            },
        )

    provider = _http_provider(handler)

    vectors = provider.embed(["one", "two"])

    assert paths == ["/v1/embeddings"]
    assert vectors == [[1.0, 0.0], [0.0, 1.0]]


def _http_provider(handler) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        base_url="https://models.example/v1",
        api_key="key",
        chat_model="chat",
        embedding_model="embed",
        transport=httpx.MockTransport(handler),
    )


def _plan_from(response: dict) -> Plan:
    return Plan.model_validate(response)


def _cosine(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=True))


def test_a_question_about_an_input_no_operation_takes_is_refused(registry) -> None:  # type: ignore[no-untyped-def]
    """A live model asked for the runtime's own integration identifier; that is never a question."""
    from oska.contracts.plan import MissingInput, Plan
    from oska.planner.planner import validate_plan

    hydrated = registry.closure("runbook.service_restart")
    invented = Plan(
        runbook_id="runbook.service_restart",
        operations=[],
        missing=[MissingInput(field="integration_id", question="Which integration id?")],
        reply_intent="ask",
    )
    honest = invented.model_copy(
        update={"missing": [MissingInput(field="service", question="Which service?")]}
    )

    refused = validate_plan(invented, hydrated)
    assert len(refused) == 1 and "'integration_id'" in refused[0] and "service" in refused[0]
    assert validate_plan(honest, hydrated) == []


def test_the_plan_schema_names_only_what_the_procedure_declares(registry) -> None:  # type: ignore[no-untyped-def]
    """A provider that constrains its output to the schema cannot name anything undeclared."""
    from oska.planner.planner import PLAN_SCHEMA, plan_schema

    hydrated = registry.closure("runbook.service_restart")
    schema = plan_schema(hydrated)
    step = schema["properties"]["operations"]["items"]["properties"]

    assert schema["properties"]["runbook_id"]["enum"] == ["runbook.service_restart"]
    assert step["integration_id"]["enum"] == [skill.id for skill in hydrated.integrations]
    assert "service.restart" in step["operation"]["enum"]
    assert "restart_deployment" not in step["operation"]["enum"]
    asked = schema["properties"]["missing"]["items"]["properties"]["field"]["enum"]
    assert "service" in asked
    assert not {"runbook_id", "integration_id", "operation"} & set(asked)
    assert "enum" not in PLAN_SCHEMA["properties"]["runbook_id"], "the shared schema is untouched"


def test_the_plan_schema_lets_a_payload_carry_the_declared_inputs(registry) -> None:  # type: ignore[no-untyped-def]
    """A payload with no properties could only be written empty under structured output."""
    from oska.planner.planner import plan_schema

    schema = plan_schema(registry.closure("runbook.service_restart"))
    payload = schema["properties"]["operations"]["items"]["properties"]["payload"]

    assert payload["additionalProperties"] is False
    assert payload["properties"]["service"]["type"] == "string"
    assert all(set(field) <= {"type", "description", "enum", "items", "format"}
               for field in payload["properties"].values())
