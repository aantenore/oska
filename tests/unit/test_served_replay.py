"""A recorded session served over HTTP: answers keyed by the request, for many sessions at once.

`oska serve --replay` answers from a cassette with no model and no key. Two
sessions served by one process interleave their calls, so an answer is found
by the request that asks for it and never by its position in the file: each
session must reach its own approval, its own verified receipt and its own
ledger hit, and a request the recording does not hold must get the answer the
runtime gives without a model rather than fail the turn.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from oska.api.app import create_app
from oska.cli.commands_serve import replay_notice
from oska.cli.main import app as cli
from oska.config import Settings
from oska.contracts.common import Message, TurnKind
from oska.planner.provider import ModelProviderError
from oska.planner.recording import KeyedReplayProvider, ReplayProvider, replay_session
from oska.router import RoutingContext, TurnRouter

REQUEST = "Il servizio search in staging è bloccato, riavvialo per favore"
CASSETTE = Path("examples") / "ops" / "cassettes" / "restart-in-italian.json"
RECEIPT = "0b5e8a3c-6f1d-4e2a-9c7b-1d2e3f4a5b6c"
LIVE_RECEIPT = "9f8e7d6c-5b4a-4321-8fed-cba987654321"

runner = CliRunner()


@pytest.fixture
def cassette(repo_root: Path) -> Path:
    return repo_root / CASSETTE


def _client(settings: Settings, provider: object) -> TestClient:
    return TestClient(create_app(settings, provider=provider))  # type: ignore[arg-type]


def _turn(client: TestClient, session: str, text: str) -> dict:
    response = client.post(f"/sessions/{session}/turns", json={"text": text})
    assert response.status_code == 200, response.text
    body: dict = response.json()
    return body


def _kinds(body: dict) -> list[str]:
    return [outcome["kind"] for outcome in body["outcomes"]]


def test_two_sessions_replaying_at_once_each_reach_approval_receipt_and_ledger_hit(
    example_settings: Settings, cassette: Path
) -> None:
    client = _client(example_settings, KeyedReplayProvider(cassette))
    sessions = ("first", "second")

    def together(text: str) -> dict[str, dict]:
        with ThreadPoolExecutor(max_workers=len(sessions)) as pool:
            bodies = pool.map(lambda session: _turn(client, session, text), sessions)
            return dict(zip(sessions, bodies, strict=True))

    asked = together(REQUEST)
    approved = together("sì")
    again = together(REQUEST)

    for session in sessions:
        assert _kinds(asked[session]) == ["read", "approval_pending"]
        pending = asked[session]["pending"]["approval"]
        assert pending["exact_payload"] == {"service": "search"}
        receipts = [o for o in approved[session]["outcomes"] if o["kind"] == "receipt"]
        assert len(receipts) == 1
        assert receipts[0]["data"]["verification"] == "verified"
        assert _kinds(again[session]) == ["ledger_hit"]
        assert again[session]["outcomes"][0]["receipt_id"] == receipts[0]["receipt_id"]
        assert receipts[0]["receipt_id"] in again[session]["reply"]
        listed = client.get(f"/sessions/{session}/receipts").json()
        assert [receipt["receipt_id"] for receipt in listed] == [receipts[0]["receipt_id"]]
    first, second = (approved[s]["outcomes"][0]["receipt_id"] for s in sessions)
    assert first != second


def test_a_request_the_recording_does_not_hold_gets_the_answer_without_a_model(
    example_settings: Settings, cassette: Path
) -> None:
    client = _client(example_settings, KeyedReplayProvider(cassette))

    body = _turn(client, "elsewhere", "che tempo fa a Roma domani?")

    assert body["reply"]
    assert all(outcome["kind"] != "receipt" for outcome in body["outcomes"])


def test_a_plan_the_recording_does_not_hold_leaves_the_turn_unplanned_not_failed(
    example_settings: Settings, cassette: Path, tmp_path: Path
) -> None:
    """The request still routes as recorded, but no recorded plan answers it."""
    steps = json.loads(cassette.read_text(encoding="utf-8"))
    for step in steps:
        if "embed" in step or "operations" not in step.get("response", {}):
            continue
        for message in step["request"]["messages"]:
            if message["content"] == REQUEST:
                message["content"] = "a request nobody made"
    altered = tmp_path / "altered.json"
    altered.write_text(json.dumps(steps), encoding="utf-8")
    client = _client(example_settings, KeyedReplayProvider(altered))

    body = _turn(client, "unplanned", REQUEST)

    assert _kinds(body) == ["invalid_plan"]
    assert body["pending"] is None


def test_the_router_answers_without_a_model_when_the_model_fails() -> None:
    class Failing:
        def route(self, text: str, context: RoutingContext) -> None:
            raise ModelProviderError("down")

    class Nothing:
        def route(self, text: str, context: RoutingContext) -> None:
            return None

    router = TurnRouter(Nothing(), Nothing(), Failing())  # type: ignore[arg-type]

    decision = router.route("anything", RoutingContext())

    assert decision.kind is TurnKind.new_task
    assert decision.source == "rule"
    assert decision.reason == "the model could not classify the turn"
    assert TurnRouter(Nothing(), Nothing(), None).route("x", RoutingContext()).reason == ""  # type: ignore[arg-type]


def _cassette(tmp_path: Path, steps: list[dict]) -> Path:
    path = tmp_path / "cassette.json"
    path.write_text(json.dumps(steps), encoding="utf-8")
    return path


def _step(messages: list[tuple[str, str]], response: dict) -> dict:
    return {
        "purpose": "Answer",
        "request": {
            "system": "Answer the question.",
            "messages": [{"role": role, "content": content} for role, content in messages],
            "schema": {},
        },
        "response": response,
    }


def _ask(provider: KeyedReplayProvider, messages: list[tuple[str, str]]) -> dict:
    return provider.complete_json(
        "Answer the question.",
        [Message(role=role, content=content) for role, content in messages],  # type: ignore[arg-type]
        {},
    )


def test_the_answer_is_the_one_whose_conversation_agrees_with_the_request(
    tmp_path: Path,
) -> None:
    provider = KeyedReplayProvider(
        _cassette(
            tmp_path,
            [
                _step([("user", "hello")], {"n": 1}),
                _step([("user", "hello"), ("assistant", "hi"), ("user", "hello")], {"n": 2}),
            ],
        )
    )

    later = [("user", "hello"), ("assistant", "hi"), ("user", "hello")]
    assert _ask(provider, later) == {"n": 2}
    assert _ask(provider, [("user", "hello")]) == {"n": 1}
    assert _ask(provider, [("user", "hello")]) == {"n": 1}


def test_steps_that_ask_the_same_are_spent_per_session(tmp_path: Path) -> None:
    provider = KeyedReplayProvider(
        _cassette(
            tmp_path,
            [_step([("user", "again")], {"n": 1}), _step([("user", "again")], {"n": 2})],
        )
    )

    with replay_session("one"):
        assert _ask(provider, [("user", "again")]) == {"n": 1}
    with replay_session("two"):
        assert _ask(provider, [("user", "again")]) == {"n": 1}
    with replay_session("one"):
        assert _ask(provider, [("user", "again")]) == {"n": 2}


def test_an_unknown_request_raises_a_provider_error_naming_the_demo(tmp_path: Path) -> None:
    provider = KeyedReplayProvider(_cassette(tmp_path, [_step([("user", "hello")], {"n": 1})]))

    with pytest.raises(ModelProviderError, match="knows only the conversation recorded"):
        _ask(provider, [("user", "something else")])


def test_a_recorded_identifier_in_the_answer_becomes_this_runs(tmp_path: Path) -> None:
    provider = KeyedReplayProvider(
        _cassette(
            tmp_path,
            [_step([("user", f"receipt {RECEIPT} landed")], {"reply": f"Receipt {RECEIPT}."})],
        )
    )

    answer = _ask(provider, [("user", f"receipt {LIVE_RECEIPT} landed")])

    assert answer == {"reply": f"Receipt {LIVE_RECEIPT}."}


def test_health_names_the_provider_in_force(example_settings: Settings, cassette: Path) -> None:
    assert _client(example_settings, KeyedReplayProvider(cassette)).get("/health").json()[
        "provider"
    ] == "replay"
    assert _client(example_settings, None).get("/health").json()["provider"] == "none"
    assert _client(example_settings, ReplayProvider(cassette)).get("/health").json()[
        "provider"
    ] == "replay"


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
    model = tmp_path / "model.json"
    model.write_text(json.dumps({"provider": "none"}), encoding="utf-8")
    return model


def _served(monkeypatch: pytest.MonkeyPatch, *args: str) -> tuple[object, dict]:
    import uvicorn

    served: dict = {}

    def _record(application: object, **options: object) -> None:
        served["application"] = application

    monkeypatch.setattr(uvicorn, "run", _record)
    return runner.invoke(cli, ["serve", *args]), served


def test_serve_replay_answers_from_the_recording_and_says_so(
    serve_env: Path, monkeypatch: pytest.MonkeyPatch, cassette: Path
) -> None:
    result, served = _served(monkeypatch, "--replay", str(cassette))

    assert result.exit_code == 0, result.output
    assert replay_notice(cassette) in result.output
    assert "nothing reaches a model" in result.output
    application = served["application"]
    assert isinstance(application.state.provider, KeyedReplayProvider)  # type: ignore[attr-defined]
    health = TestClient(application).get("/health").json()  # type: ignore[arg-type]
    assert health["provider"] == "replay"


@pytest.mark.parametrize(
    "configure",
    [
        lambda model, env: model.write_text(
            json.dumps({"provider": "openai_compatible", "base_url": "https://flat.example/v1",
                        "chat_model": "c", "embedding_model": "e"}),
            encoding="utf-8",
        ),
        lambda model, env: env.setenv("OSKA_PROVIDER", "openai_compatible"),
    ],
    ids=["model-file", "environment"],
)
def test_serve_replay_is_refused_beside_a_configured_provider(
    serve_env: Path,
    monkeypatch: pytest.MonkeyPatch,
    cassette: Path,
    configure: Callable[[Path, pytest.MonkeyPatch], object],
) -> None:
    configure(serve_env, monkeypatch)

    result, served = _served(monkeypatch, "--replay", str(cassette))

    assert result.exit_code == 1
    assert "application" not in served
    assert "--replay answers from a recording" in result.output
    assert "openai_compatible" in result.output
    assert len(result.output.strip().splitlines()) == 1


def test_serve_replay_of_a_missing_cassette_stops_with_one_line(
    serve_env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    result, served = _served(monkeypatch, "--replay", str(tmp_path / "missing.json"))

    assert result.exit_code == 1
    assert "application" not in served
    assert "the recorded session could not be read" in result.output
    assert "Traceback" not in result.output
