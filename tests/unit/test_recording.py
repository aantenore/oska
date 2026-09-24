"""Recording a session to a cassette and replaying it offline."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from oska.cli.main import app
from oska.config import Settings
from oska.contracts.common import Message
from oska.engine.factory import build_engine
from oska.planner.provider import ModelProvider, ModelProviderError
from oska.planner.recording import (
    RecordingProvider,
    ReplayProvider,
    cassette_to_script,
    read_cassette,
)
from oska.planner.scripted import ScriptedModelProvider, hash_bag_vector

runner = CliRunner()

RESTART = "the search service in staging needs a restart, it is wedged again"
GREETING_SCHEMA: dict = {"type": "object", "properties": {"reply": {"type": "string"}}}
TITLED_SCHEMA: dict = {"title": "route", "type": "object"}

SCRIPT: list[dict] = [
    {"purpose": "first", "expect_contains": "restart", "response": {"kind": "new_task"}},
    {"purpose": "second", "response": {"reply": "done"}},
]


@pytest.fixture
def scenario_dir(repo_root: Path) -> Path:
    """The example scenario directory."""
    return repo_root / "examples" / "ops" / "scenarios"


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, repo_root: Path, tmp_path: Path) -> Path:
    """Point the command line at the example domain, with state under the test."""
    state = tmp_path / "state"
    for name, value in (
        ("SKILLS_ROOTS", repo_root / "examples" / "ops" / "skills"),
        ("RUNTIME_CONFIG", repo_root / "configs" / "runtime.json"),
        ("MODEL_CONFIG", repo_root / "configs" / "model.json"),
        ("CONNECTORS_CONFIG", repo_root / "examples" / "ops" / "configs" / "connectors.json"),
        ("SCHEMAS_DIR", repo_root / "schemas"),
        ("SCENARIOS_DIR", repo_root / "examples" / "ops" / "scenarios"),
        ("STATE_DIR", state),
    ):
        monkeypatch.setenv(f"OSKA_{name}", str(value))
    monkeypatch.setenv("OSKA_PROVIDER", "none")
    return state


def _ask(provider: ModelProvider, text: str, schema: dict | None = None) -> dict:
    return provider.complete_json(
        "Classify one message from a conversation about running declared procedures.",
        [Message(role="user", content=text)],
        schema if schema is not None else GREETING_SCHEMA,
    )


def test_a_cassette_holds_the_request_and_the_answer_of_every_call(tmp_path: Path) -> None:
    path = tmp_path / "cassette.json"
    provider = RecordingProvider(ScriptedModelProvider(SCRIPT), path)

    _ask(provider, RESTART)
    provider.embed(["one text"])

    steps = read_cassette(path)
    assert len(steps) == 2
    assert steps[0]["purpose"] == "Classify one message from a conversation about running"
    assert steps[0]["expect_contains"] == RESTART[:60]
    assert steps[0]["request"]["messages"] == [{"role": "user", "content": RESTART}]
    assert steps[0]["request"]["schema"] == GREETING_SCHEMA
    assert steps[0]["response"] == {"kind": "new_task"}
    assert steps[1]["embed"]["texts"] == ["one text"]
    assert steps[1]["embed"]["vectors"] == [hash_bag_vector("one text")]


def test_the_schema_title_names_the_call_when_it_has_one(tmp_path: Path) -> None:
    path = tmp_path / "cassette.json"
    provider = RecordingProvider(ScriptedModelProvider(SCRIPT), path)

    _ask(provider, RESTART, TITLED_SCHEMA)

    assert read_cassette(path)[0]["purpose"] == "route"


def test_the_cassette_is_on_disk_after_every_call(tmp_path: Path) -> None:
    path = tmp_path / "cassette.json"
    provider = RecordingProvider(ScriptedModelProvider(SCRIPT), path)

    _ask(provider, RESTART)
    assert len(read_cassette(path)) == 1

    _ask(provider, "anything else")
    assert len(read_cassette(path)) == 2


def test_recording_twice_adds_to_the_cassette_it_finds(tmp_path: Path) -> None:
    path = tmp_path / "cassette.json"
    _ask(RecordingProvider(ScriptedModelProvider(SCRIPT), path), RESTART)
    _ask(RecordingProvider(ScriptedModelProvider(SCRIPT), path), RESTART)

    assert len(read_cassette(path)) == 2


def test_replay_answers_what_was_recorded(tmp_path: Path) -> None:
    path = tmp_path / "cassette.json"
    recorder = RecordingProvider(ScriptedModelProvider(SCRIPT), path)
    recorded = [_ask(recorder, RESTART), _ask(recorder, "second question")]
    vectors = recorder.embed(["one text", "another text"])

    replay = ReplayProvider(path)

    assert [_ask(replay, RESTART), _ask(replay, "second question")] == recorded
    assert replay.embed(["one text", "another text"]) == vectors
    assert replay.remaining() == 0


def test_replay_refuses_a_request_the_recording_did_not_hold(tmp_path: Path) -> None:
    path = tmp_path / "cassette.json"
    _ask(RecordingProvider(ScriptedModelProvider(SCRIPT), path), RESTART)
    replay = ReplayProvider(path)

    with pytest.raises(ModelProviderError) as raised:
        _ask(replay, "a completely different request")

    message = str(raised.value)
    assert str(path) in message
    assert "expected the last user message to contain" in message
    assert "a completely different request" in message


def test_replay_says_when_the_cassette_runs_out(tmp_path: Path) -> None:
    path = tmp_path / "cassette.json"
    _ask(RecordingProvider(ScriptedModelProvider(SCRIPT), path), RESTART)
    replay = ReplayProvider(path)
    _ask(replay, RESTART)

    with pytest.raises(ModelProviderError) as raised:
        _ask(replay, RESTART)

    assert "exhausted after 1 step(s)" in str(raised.value)


def test_replay_embeds_a_text_the_cassette_never_saw(tmp_path: Path) -> None:
    path = tmp_path / "cassette.json"
    recorder = RecordingProvider(ScriptedModelProvider(SCRIPT), path)
    recorder.embed(["a recorded text"])

    vectors = ReplayProvider(path).embed(["a recorded text", "a text nobody recorded"])

    assert vectors[0] == hash_bag_vector("a recorded text")
    assert vectors[1] == hash_bag_vector("a text nobody recorded")
    assert len(vectors[1]) == len(vectors[0])


def test_the_fallback_matches_the_width_of_the_recorded_vectors(tmp_path: Path) -> None:
    path = tmp_path / "cassette.json"
    path.write_text(
        json.dumps([{"embed": {"texts": ["known"], "vectors": [[0.1, 0.2, 0.3]]}}]),
        encoding="utf-8",
    )

    vectors = ReplayProvider(path).embed(["known", "unknown"])

    assert vectors[0] == [0.1, 0.2, 0.3]
    assert len(vectors[1]) == 3


def test_a_file_that_is_not_a_cassette_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "cassette.json"
    path.write_text('{"provider": "none"}', encoding="utf-8")

    with pytest.raises(ModelProviderError):
        ReplayProvider(path)


def test_a_script_keeps_only_what_a_scenario_reads(tmp_path: Path) -> None:
    path = tmp_path / "cassette.json"
    recorder = RecordingProvider(ScriptedModelProvider(SCRIPT), path)
    _ask(recorder, RESTART)
    recorder.embed(["one text"])

    script = cassette_to_script(path)

    assert script == [
        {
            "purpose": "Classify one message from a conversation about running",
            "response": {"kind": "new_task"},
            "expect_contains": RESTART[:60],
        }
    ]


def _record_a_turn(settings: Settings, path: Path, script: Path, session: str) -> str:
    """Run one turn against a scripted provider, recording every call."""
    provider = RecordingProvider(ScriptedModelProvider(script), path)
    engine = build_engine(settings, provider=provider)
    return engine.turn(session, RESTART).reply


def test_a_recorded_turn_replays_the_same_way(
    example_settings: Settings, scenario_dir: Path, tmp_path: Path
) -> None:
    path = tmp_path / "cassette.json"
    recorded = _record_a_turn(
        example_settings, path, scenario_dir / "02_restart_staging.json", "recorded"
    )

    replayed_settings = example_settings.model_copy(update={"state_dir": tmp_path / "replayed"})
    engine = build_engine(replayed_settings, provider=ReplayProvider(path))
    result = engine.turn("replayed", RESTART)

    assert result.reply == recorded
    assert [outcome.kind for outcome in result.outcomes] == ["read", "approval_pending"]


def test_the_script_of_a_cassette_drives_a_scenario_turn(
    example_settings: Settings, scenario_dir: Path, tmp_path: Path
) -> None:
    path = tmp_path / "cassette.json"
    recorded = _record_a_turn(
        example_settings, path, scenario_dir / "02_restart_staging.json", "recorded"
    )

    scripted_settings = example_settings.model_copy(update={"state_dir": tmp_path / "scripted"})
    engine = build_engine(
        scripted_settings, provider=ScriptedModelProvider(cassette_to_script(path))
    )
    result = engine.turn("scripted", RESTART)

    assert result.reply == recorded
    assert result.pending is not None
    assert result.pending.kind == "approval"


def test_run_records_a_cassette_and_replays_it(
    cli_env: Path, scenario_dir: Path, tmp_path: Path
) -> None:
    path = tmp_path / "cassettes" / "restart.json"
    recorded = runner.invoke(
        app,
        [
            "run",
            "--session",
            "recorded",
            "--text",
            RESTART,
            "--provider",
            "scripted",
            "--script",
            str(scenario_dir / "02_restart_staging.json"),
            "--record",
            str(path),
        ],
    )
    assert recorded.exit_code == 0
    assert path.is_file()

    replayed = runner.invoke(
        app, ["run", "--session", "replayed", "--text", RESTART, "--replay", str(path)]
    )

    assert replayed.exit_code == 0
    assert _without_identifiers(replayed.stdout) == _without_identifiers(recorded.stdout)


def _without_identifiers(output: str) -> str:
    """The same output with the approval identifier dropped, which is new each run."""
    return "\n".join(
        line.rsplit(" ", 1)[0] if line.startswith("awaiting approval ") else line
        for line in output.splitlines()
    )


def test_recording_needs_a_provider(cli_env: Path, tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["run", "--text", RESTART, "--provider", "none", "--record", str(tmp_path / "c.json")],
    )

    assert result.exit_code == 1
    assert "the provider in force is none" in result.stderr
    assert "Traceback" not in result.stderr


def test_recording_and_replaying_are_not_asked_for_together(cli_env: Path, tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "run",
            "--text",
            RESTART,
            "--record",
            str(tmp_path / "c.json"),
            "--replay",
            str(tmp_path / "c.json"),
        ],
    )

    assert result.exit_code == 1
    assert "use one of them" in result.stderr


def test_replaying_a_cassette_that_is_not_there_leaves_one_line(
    cli_env: Path, tmp_path: Path
) -> None:
    result = runner.invoke(
        app, ["run", "--text", RESTART, "--replay", str(tmp_path / "absent.json")]
    )

    assert result.exit_code == 1
    assert "the cassette could not be read" in result.stderr
    assert "Traceback" not in result.stderr


def test_cassette_to_script_prints_the_scenario_script(tmp_path: Path) -> None:
    path = tmp_path / "cassette.json"
    recorder = RecordingProvider(ScriptedModelProvider(SCRIPT), path)
    _ask(recorder, RESTART)
    recorder.embed(["one text"])

    result = runner.invoke(app, ["cassette", "to-script", str(path)])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == cassette_to_script(path)


def test_cassette_to_script_writes_the_file_it_is_given(tmp_path: Path) -> None:
    path = tmp_path / "cassette.json"
    _ask(RecordingProvider(ScriptedModelProvider(SCRIPT), path), RESTART)
    out = tmp_path / "scripts" / "restart.json"

    result = runner.invoke(app, ["cassette", "to-script", str(path), "--out", str(out)])

    assert result.exit_code == 0
    assert "1 step(s)" in result.stdout
    assert json.loads(out.read_text(encoding="utf-8")) == cassette_to_script(path)
