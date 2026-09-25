"""More than one model provider: profiles, roles, fallback, the messages adapter and the wiring.

Every test here is offline. Each profile is a host of its own behind one mock
transport, so a test can make one endpoint fail and watch the next one answer.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from oska.audit.sink import MemorySink
from oska.cli.main import app
from oska.config import ModelConfig, Settings, load_model_config
from oska.contracts.common import Message
from oska.engine.budget import BudgetedProvider, BudgetExceeded, ModelBudget
from oska.engine.factory import build_engine
from oska.planner.anthropic import AnthropicProvider
from oska.planner.chain import FallbackProvider
from oska.planner.openai_compatible import OpenAICompatibleProvider
from oska.planner.provider import ModelProviderError, provider_embeds
from oska.planner.providers import build_provider

runner = CliRunner()

CONVERSATION = [Message(role="user", content="restart the checkout service")]
SCHEMA = {"type": "object", "properties": {"reply": {"type": "string"}}}
ANSWER = {"reply": "done"}
PRIMARY_KEY = "primary-secret-value"
SECONDARY_KEY = "secondary-secret-value"
RESTART_REQUEST = "the search service in staging needs a restart, it is wedged again"
CONTAINER_RUNTIME = "integration.container_runtime"

ROUTE = {
    "kind": "new_task",
    "runbook_id": "runbook.service_restart",
    "confidence": 0.9,
    "language": "en",
}
PLAN = {
    "runbook_id": "runbook.service_restart",
    "operations": [
        {
            "integration_id": CONTAINER_RUNTIME,
            "operation": "service.status",
            "payload": {"service": "search"},
            "write": False,
            "purpose": "read the current state",
        },
        {
            "integration_id": CONTAINER_RUNTIME,
            "operation": "service.restart",
            "payload": {"service": "search"},
            "write": True,
            "purpose": "restart the service",
        },
    ],
    "missing": [],
    "reply_intent": "report the restart",
}

Handler = Callable[[httpx.Request], httpx.Response]


def _profiles_config(**overrides: object) -> dict:
    document: dict = {
        "provider": "profiles",
        "profiles": {
            "primary": {
                "adapter": "openai_compatible",
                "base_url": "https://primary.example/v1",
                "api_key_env": "PRIMARY_KEY",
                "chat_model": "chat-primary",
            },
            "secondary": {
                "adapter": "openai_compatible",
                "base_url": "https://secondary.example/v1",
                "api_key_env": "SECONDARY_KEY",
                "chat_model": "chat-secondary",
                "embedding_model": "embed-secondary",
            },
        },
        "roles": {"chat": ["primary", "secondary"], "embeddings": []},
    }
    document.update(overrides)
    return document


def _chat(answer: dict) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(answer)}}]})


def _chain(handler: Handler, env: dict[str, str] | None = None, **overrides: object):
    warnings: list[str] = []
    provider = build_provider(
        ModelConfig.model_validate(_profiles_config(**overrides)),
        env={"PRIMARY_KEY": PRIMARY_KEY, "SECONDARY_KEY": SECONDARY_KEY} if env is None else env,
        warn=warnings.append,
        transport=httpx.MockTransport(handler),
    )
    return provider, warnings


# Configuration


def test_profiles_and_roles_are_read_from_the_model_configuration() -> None:
    config = ModelConfig.model_validate(_profiles_config())

    assert config.provider == "profiles"
    assert config.roles is not None and config.roles.chat == ["primary", "secondary"]
    assert config.profiles["secondary"].embedding_model == "embed-secondary"
    assert config.profiles["primary"].max_tokens is None


@pytest.mark.parametrize(
    ("overrides", "complaint"),
    [
        ({"roles": {"chat": ["primary", "missing"]}}, "not declared: missing"),
        ({"roles": {"chat": ["primary"], "embeddings": ["ghost"]}}, "not declared: ghost"),
        ({"roles": {"chat": []}}, "roles.chat"),
        ({"roles": None}, "roles.chat"),
        ({"roles": {"chat": ["secondary"], "embeddings": ["primary"]}}, "embedding_model"),
    ],
)
def test_roles_that_do_not_hold_up_are_refused_at_load(overrides: dict, complaint: str) -> None:
    with pytest.raises(ValidationError, match=complaint):
        ModelConfig.model_validate(_profiles_config(**overrides))


def test_a_key_written_into_a_profile_is_refused() -> None:
    document = _profiles_config()
    document["profiles"]["primary"]["api_key"] = "written-into-the-file"

    with pytest.raises(ValidationError, match="api_key_env"):
        ModelConfig.model_validate(document)


def test_the_flat_shape_and_its_overrides_are_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "model.json"
    path.write_text(
        json.dumps({"provider": "none", "base_url": None, "api_key": None,
                    "chat_model": "chat-model-name", "embedding_model": "embedding-model-name",
                    "timeout_seconds": 30.0}),
        encoding="utf-8",
    )
    env = {
        "OSKA_PROVIDER": "openai_compatible",
        "OSKA_MODEL_BASE_URL": "https://flat.example/v1",
        "OSKA_MODEL_API_KEY": "flat-key",
        "OSKA_MODEL_CHAT": "chat-flat",
        "OSKA_MODEL_EMBEDDING": "embed-flat",
    }

    config = load_model_config(path, env=env)

    assert (config.provider, config.base_url, config.api_key) == (
        "openai_compatible", "https://flat.example/v1", "flat-key"
    )
    assert (config.chat_model, config.embedding_model) == ("chat-flat", "embed-flat")
    assert config.profiles == {} and config.roles is None


def test_the_flat_shape_sends_exactly_the_body_it_always_sent() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _chat(ANSWER)

    config = ModelConfig(
        provider="openai_compatible",
        base_url="https://flat.example/v1",
        api_key="flat-key",
        chat_model="chat-flat",
        embedding_model="embed-flat",
    )
    provider = build_provider(config, transport=httpx.MockTransport(handler))

    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider.complete_json("system", CONVERSATION, SCHEMA) == ANSWER
    assert json.loads(seen[0].content) == {
        "model": "chat-flat",
        "temperature": 0.0,
        "messages": [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "restart the checkout service"},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "response", "schema": SCHEMA},
        },
    }
    assert seen[0].headers["authorization"] == "Bearer flat-key"
    assert str(seen[0].url) == "https://flat.example/v1/chat/completions"


def test_none_builds_nothing_and_the_flat_messages_shape_builds_that_adapter() -> None:
    assert build_provider(ModelConfig()) is None
    built = build_provider(
        ModelConfig(provider="anthropic", base_url="https://messages.example", chat_model="c")
    )
    assert isinstance(built, AnthropicProvider)
    assert provider_embeds(built) is False


# Keys


def test_a_profile_key_is_read_from_its_variable_and_sent() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _chat(ANSWER)

    provider, warnings = _chain(handler)

    assert provider.complete_json("system", CONVERSATION, SCHEMA) == ANSWER
    assert seen[0].headers["authorization"] == f"Bearer {PRIMARY_KEY}"
    assert warnings == []


def test_a_profile_without_its_key_is_skipped_with_a_warning_that_names_only_the_variable(
) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        return _chat(ANSWER)

    provider, warnings = _chain(handler, env={"PRIMARY_KEY": "  ", "SECONDARY_KEY": SECONDARY_KEY})

    assert provider.complete_json("system", CONVERSATION, SCHEMA) == ANSWER
    assert seen == ["secondary.example"]
    assert len(warnings) == 1
    assert "PRIMARY_KEY" in warnings[0] and "primary" in warnings[0]
    assert SECONDARY_KEY not in warnings[0]


def test_the_skip_warning_goes_to_standard_error_without_the_value(
    capsys: pytest.CaptureFixture[str],
) -> None:
    build_provider(
        ModelConfig.model_validate(_profiles_config()),
        env={"SECONDARY_KEY": SECONDARY_KEY},
        transport=httpx.MockTransport(lambda request: _chat(ANSWER)),
    )

    captured = capsys.readouterr()
    assert "PRIMARY_KEY" in captured.err
    assert SECONDARY_KEY not in captured.err + captured.out


def test_no_usable_chat_profile_names_the_missing_variables() -> None:
    with pytest.raises(ModelProviderError, match="PRIMARY_KEY, SECONDARY_KEY"):
        _chain(lambda request: _chat(ANSWER), env={})


# Fallback


def _failing_primary(failure: Callable[[httpx.Request], httpx.Response]) -> tuple[Handler, list]:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        if request.url.host == "primary.example":
            return failure(request)
        return _chat(ANSWER)

    return handler, seen


def _timeout(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("timed out", request=request)


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        (lambda request: httpx.Response(503, json={"error": {"message": "down"}}), "HTTP 503"),
        (lambda request: httpx.Response(429, json={"error": {"message": "slow"}}), "HTTP 429"),
        (_timeout, "ReadTimeout"),
        (lambda request: httpx.Response(200, json={"choices": []}), "ModelProviderError"),
    ],
)
def test_a_retryable_failure_moves_to_the_next_chat_profile(failure, reason: str) -> None:  # type: ignore[no-untyped-def]
    from oska.planner.chain import recording_profiles

    handler, seen = _failing_primary(failure)
    provider, _ = _chain(handler)

    with recording_profiles() as log:
        assert provider.complete_json("system", CONVERSATION, SCHEMA) == ANSWER

    assert seen == ["primary.example", "secondary.example"]
    assert log.fallbacks == [
        {"role": "chat", "from": "primary", "to": "secondary", "reason": reason}
    ]
    assert log.answered == [{"role": "chat", "profile": "secondary"}]


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_a_request_refused_as_written_raises_without_trying_another_profile(status: int) -> None:
    handler, seen = _failing_primary(
        lambda request: httpx.Response(status, json={"error": {"message": "refused"}})
    )
    provider, _ = _chain(handler)

    with pytest.raises(ModelProviderError, match="refused") as raised:
        provider.complete_json("system", CONVERSATION, SCHEMA)

    assert raised.value.retryable is False
    assert seen == ["primary.example"]


def test_the_last_profile_failing_raises_its_error() -> None:
    provider, _ = _chain(lambda request: httpx.Response(503, json={"error": "down"}))

    with pytest.raises(ModelProviderError, match="503") as raised:
        provider.complete_json("system", CONVERSATION, SCHEMA)

    assert raised.value.retryable is True


def test_embeddings_follow_their_own_role_and_say_whether_there_is_one() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "secondary.example"
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0, 0.0]}]})

    without, _ = _chain(handler)
    with_role, _ = _chain(handler, roles={"chat": ["primary"], "embeddings": ["secondary"]})

    assert provider_embeds(without) is False
    assert provider_embeds(BudgetedProvider(without, ModelBudget(1))) is False
    assert provider_embeds(with_role) is True
    assert provider_embeds(BudgetedProvider(with_role, ModelBudget(1))) is True
    assert with_role.embed(["one"]) == [[1.0, 0.0]]
    with pytest.raises(ModelProviderError):
        without.embed(["one"])


def test_a_call_that_fell_back_counts_once_against_the_turn_budget() -> None:
    handler, seen = _failing_primary(lambda request: httpx.Response(503, json={"error": "x"}))
    provider, _ = _chain(handler)
    budget = ModelBudget(1)
    budgeted = BudgetedProvider(provider, budget)

    with budget.for_turn():
        assert budgeted.complete_json("system", CONVERSATION, SCHEMA) == ANSWER
        assert budget.spent == 1
        assert budget.fallbacks() is not None
        with pytest.raises(BudgetExceeded):
            budgeted.complete_json("system", CONVERSATION, SCHEMA)

    assert seen == ["primary.example", "secondary.example"]
    assert budget.fallbacks() is None


# The turn


def _turn_handler(seen: list[str], *, primary_down: bool = True) -> Handler:
    """Each profile routes, plans and phrases like a model would; the primary may be down."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.url.host}{request.url.path}")
        if primary_down and request.url.host == "primary.example":
            return httpx.Response(503, json={"error": {"message": "unavailable"}})
        body = json.loads(request.content)
        properties = json.dumps(body.get("response_format", {}))
        if "operations" in properties:
            return _chat(PLAN)
        if "confidence" in properties:
            return _chat(ROUTE)
        return _chat({"reply": "Restarting search once you approve.", "action_labels": {}})

    return handler


def _engine_settings(repo_root: Path, tmp_path: Path, calls: int) -> Settings:
    document = json.loads((repo_root / "configs" / "runtime.json").read_text(encoding="utf-8"))
    document["turn"] = {"max_model_calls": calls}
    runtime = tmp_path / "runtime.json"
    runtime.write_text(json.dumps(document), encoding="utf-8")
    return Settings(
        skills_roots=[repo_root / "examples" / "ops" / "skills"],
        runtime_config=runtime,
        model_config_path=repo_root / "configs" / "model.json",
        connectors_config=repo_root / "examples" / "ops" / "configs" / "connectors.json",
        schemas_dir=repo_root / "schemas",
        state_dir=tmp_path / "state",
        provider="none",
    )


def test_a_turn_that_fell_back_leaves_one_audit_event_naming_profiles_only(
    repo_root: Path, tmp_path: Path
) -> None:
    seen: list[str] = []
    provider, _ = _chain(_turn_handler(seen))
    # Route and plan: two calls, each answered only after a fallback. Counted
    # once each they fit a budget of two; counted per attempt they would not.
    engine = build_engine(
        _engine_settings(repo_root, tmp_path, calls=2), provider=provider, audit_sink=MemorySink()
    )

    result = engine.turn("s", RESTART_REQUEST)

    assert len(seen) == 4
    assert result.pending is not None and result.pending.kind == "approval"
    assert [event for event in result.events if event.kind == "budget"] == []
    fallbacks = [event for event in result.events if event.kind == "model_fallback"]
    assert len(fallbacks) == 1
    data = fallbacks[0].data
    assert data["fallbacks"] and all(
        entry == {"role": "chat", "from": "primary", "to": "secondary", "reason": "HTTP 503"}
        for entry in data["fallbacks"]
    )
    assert data["answered"] == [{"role": "chat", "profile": "secondary"}] * 2
    assert "search" not in json.dumps(data) and "restart" not in json.dumps(data)
    assert not [path for path in seen if path.endswith("/embeddings")]


def test_a_turn_without_a_fallback_leaves_no_fallback_event(
    repo_root: Path, tmp_path: Path
) -> None:
    seen: list[str] = []
    provider, _ = _chain(_turn_handler(seen, primary_down=False))
    engine = build_engine(
        _engine_settings(repo_root, tmp_path, calls=3), provider=provider, audit_sink=MemorySink()
    )

    result = engine.turn("s", RESTART_REQUEST)

    assert [event for event in result.events if event.kind == "model_fallback"] == []
    assert seen and all(path.startswith("primary.example") for path in seen)


def test_a_provider_that_does_not_embed_ranks_the_catalog_by_keyword(
    repo_root: Path, tmp_path: Path
) -> None:
    from oska.index.keyword import KeywordIndex

    provider, _ = _chain(lambda request: _chat(ANSWER))
    engine = build_engine(_engine_settings(repo_root, tmp_path, calls=3), provider=provider)

    assert isinstance(engine._discovery._index, KeywordIndex)  # noqa: SLF001


# The chat-completions adapter


def test_structured_output_steps_down_to_json_object_then_to_none() -> None:
    seen: list[dict] = []
    refusals = iter(
        [
            httpx.Response(400, json={"error": {"message": "json_schema is not supported"}}),
            httpx.Response(422, json={"error": {"message": "Structured outputs unavailable"}}),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return next(refusals, None) or _chat(ANSWER)

    provider = OpenAICompatibleProvider(
        "https://flat.example/v1", chat_model="c", transport=httpx.MockTransport(handler)
    )

    assert provider.complete_json("system", CONVERSATION, SCHEMA) == ANSWER
    assert seen[0]["response_format"]["type"] == "json_schema"
    assert seen[0]["messages"][0]["content"] == "system"
    assert seen[1]["response_format"] == {"type": "json_object"}
    assert json.dumps(SCHEMA) in seen[1]["messages"][0]["content"]
    assert "response_format" not in seen[2]
    assert json.dumps(SCHEMA) in seen[2]["messages"][0]["content"]


def test_a_refusal_that_is_not_about_the_format_is_not_stepped_around() -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(400, json={"error": {"message": "unknown model"}})

    provider = OpenAICompatibleProvider(
        "https://flat.example/v1", chat_model="c", transport=httpx.MockTransport(handler)
    )

    with pytest.raises(ModelProviderError, match="unknown model"):
        provider.complete_json("system", CONVERSATION, SCHEMA)
    assert len(seen) == 1


def test_a_profile_sends_its_headers_and_its_bound_on_the_answer() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _chat(ANSWER)

    document = _profiles_config()
    document["profiles"]["primary"]["headers"] = {"x-route": "blue"}
    document["profiles"]["primary"]["max_tokens"] = 512
    provider = build_provider(
        ModelConfig.model_validate(document),
        env={"PRIMARY_KEY": PRIMARY_KEY},
        warn=lambda message: None,
        transport=httpx.MockTransport(handler),
    )

    provider.complete_json("system", CONVERSATION, SCHEMA)

    assert seen[0].headers["x-route"] == "blue"
    assert json.loads(seen[0].content)["max_tokens"] == 512


# The messages adapter


def _messages_provider(handler: Handler, **options: object) -> AnthropicProvider:
    return AnthropicProvider(
        "https://messages.example",
        api_key="messages-key",
        chat_model="chat-messages",
        transport=httpx.MockTransport(handler),
        **options,  # type: ignore[arg-type]
    )


def _tool_answer(answer: dict) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "content": [
                {"type": "text", "text": "thinking aloud"},
                {"type": "tool_use", "name": "respond", "input": answer},
            ]
        },
    )


def test_the_messages_adapter_asks_through_one_required_tool() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _tool_answer(ANSWER)

    provider = _messages_provider(handler)
    conversation = [
        Message(role="system", content="first rule"),
        Message(role="assistant", content="earlier answer"),
        Message(role="user", content="one"),
        Message(role="user", content="two"),
        Message(role="system", content="second rule"),
        Message(role="assistant", content="three"),
    ]

    assert provider.complete_json("base", conversation, SCHEMA, temperature=0.2) == ANSWER

    request = seen[0]
    body = json.loads(request.content)
    assert str(request.url) == "https://messages.example/v1/messages"
    assert request.headers["x-api-key"] == "messages-key"
    assert request.headers["anthropic-version"] == "2023-06-01"
    assert request.headers["content-type"] == "application/json"
    assert "authorization" not in request.headers
    assert body["model"] == "chat-messages"
    assert body["max_tokens"] == 4096
    assert body["temperature"] == 0.2
    assert body["system"] == "base\n\nfirst rule\n\nsecond rule"
    assert body["tools"] == [
        {"name": "respond", "description": body["tools"][0]["description"], "input_schema": SCHEMA}
    ]
    assert body["tool_choice"] == {"type": "tool", "name": "respond"}
    assert [turn["role"] for turn in body["messages"]] == ["user", "assistant", "user", "assistant"]
    assert body["messages"][2]["content"] == "one\n\ntwo"


def test_the_messages_adapter_honors_a_profile_bound_and_headers() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _tool_answer(ANSWER)

    provider = _messages_provider(handler, max_tokens=256, headers={"anthropic-version": "v2"})
    provider.complete_json("base", CONVERSATION, SCHEMA)

    assert json.loads(seen[0].content)["max_tokens"] == 256
    assert seen[0].headers["anthropic-version"] == "v2"


@pytest.mark.parametrize(
    ("response", "retryable"),
    [
        (httpx.Response(200, json={"content": [{"type": "text", "text": "no tool"}]}), True),
        (httpx.Response(529, json={"error": {"message": "overloaded now"}}), True),
        (httpx.Response(401, json={"error": {"message": "bad credentials"}}), False),
    ],
)
def test_the_messages_adapter_reports_failures_readably(
    response: httpx.Response, retryable: bool
) -> None:
    provider = _messages_provider(lambda request: response)

    with pytest.raises(ModelProviderError) as raised:
        provider.complete_json("base", CONVERSATION, SCHEMA)

    assert raised.value.retryable is retryable
    if response.status_code >= 400:
        assert response.json()["error"]["message"] in str(raised.value)


def test_the_messages_adapter_serves_no_embeddings() -> None:
    provider = _messages_provider(lambda request: _tool_answer(ANSWER))

    assert provider_embeds(provider) is False
    with pytest.raises(ModelProviderError) as raised:
        provider.embed(["one"])
    assert raised.value.retryable is False


def test_a_messages_profile_falls_back_to_a_completions_profile() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "messages.example":
            return httpx.Response(500, json={"error": {"message": "internal"}})
        return _chat(ANSWER)

    document = _profiles_config()
    document["profiles"]["primary"] = {
        "adapter": "anthropic",
        "base_url": "https://messages.example",
        "api_key_env": "PRIMARY_KEY",
        "chat_model": "chat-messages",
    }
    provider = build_provider(
        ModelConfig.model_validate(document),
        env={"PRIMARY_KEY": PRIMARY_KEY, "SECONDARY_KEY": SECONDARY_KEY},
        transport=httpx.MockTransport(handler),
    )

    assert isinstance(provider, FallbackProvider)
    assert provider.complete_json("system", CONVERSATION, SCHEMA) == ANSWER


# Serving


@pytest.fixture
def serve_env(monkeypatch: pytest.MonkeyPatch, repo_root: Path, tmp_path: Path) -> Path:
    """The command line pointed at the example domain, with a model file of the test's own."""
    for name, value in (
        ("SKILLS_ROOTS", repo_root / "examples" / "ops" / "skills"),
        ("RUNTIME_CONFIG", repo_root / "configs" / "runtime.json"),
        ("CONNECTORS_CONFIG", repo_root / "examples" / "ops" / "configs" / "connectors.json"),
        ("SCHEMAS_DIR", repo_root / "schemas"),
        ("STATE_DIR", tmp_path / "state"),
        ("MODEL_CONFIG", tmp_path / "model.json"),
    ):
        monkeypatch.setenv(f"OSKA_{name}", str(value))
    for name in ("PROVIDER", "PLATFORM", "MODEL_BASE_URL", "MODEL_API_KEY", "MODEL_CHAT",
                 "MODEL_EMBEDDING"):
        monkeypatch.delenv(f"OSKA_{name}", raising=False)
    return tmp_path / "model.json"


def _served(monkeypatch: pytest.MonkeyPatch, *args: str) -> tuple[object, dict]:
    import uvicorn

    served: dict = {}

    def _record(application: object, **options: object) -> None:
        served["application"] = application

    monkeypatch.setattr(uvicorn, "run", _record)
    return runner.invoke(app, ["serve", *args]), served


def test_serve_reasons_with_the_configured_provider(
    serve_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    serve_env.write_text(
        json.dumps({"provider": "openai_compatible", "base_url": "https://flat.example/v1",
                    "chat_model": "c", "embedding_model": "e"}),
        encoding="utf-8",
    )

    result, served = _served(monkeypatch)

    assert result.exit_code == 0, result.output
    application = served["application"]
    assert isinstance(application.state.provider, OpenAICompatibleProvider)
    assert application.state.services.engine.planner is not None


def test_serve_with_no_provider_serves_without_one(
    serve_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    serve_env.write_text(json.dumps({"provider": "none"}), encoding="utf-8")

    result, served = _served(monkeypatch)

    assert result.exit_code == 0, result.output
    assert served["application"].state.provider is None
    assert served["application"].state.services.engine.planner is None


def test_serve_hands_the_provider_to_every_domain_of_a_platform(
    serve_env: Path, monkeypatch: pytest.MonkeyPatch, examples: Path
) -> None:
    serve_env.write_text(
        json.dumps({"provider": "openai_compatible", "base_url": "https://flat.example/v1",
                    "chat_model": "c", "embedding_model": "e"}),
        encoding="utf-8",
    )

    result, served = _served(monkeypatch, "--platform", str(examples / "platform.yaml"))

    assert result.exit_code == 0, result.output
    assert served["application"].state.services.engine.planner is not None


def test_serve_stops_with_one_line_when_the_provider_cannot_be_built(
    serve_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PRIMARY_KEY", raising=False)
    monkeypatch.delenv("SECONDARY_KEY", raising=False)
    serve_env.write_text(json.dumps(_profiles_config()), encoding="utf-8")

    result, served = _served(monkeypatch)

    assert result.exit_code == 1
    assert "application" not in served
    assert "the model provider could not be built" in result.output
    assert "Traceback" not in result.output
