"""Recording a session to a cassette and replaying it offline."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from oska.cli.commands_run import ReplayProgress
from oska.cli.main import app
from oska.config import Settings, load_settings
from oska.contracts.common import Message
from oska.engine.factory import build_engine
from oska.planner.provider import ModelProvider, ModelProviderError, provider_embeds
from oska.planner.recording import (
    RecordingProvider,
    ReplayProvider,
    cassette_to_script,
    read_cassette,
)
from oska.planner.scripted import ScriptedModelProvider, ScriptMismatchError, hash_bag_vector

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

    with pytest.raises(ScriptMismatchError) as raised:
        _ask(replay, "a completely different request")

    message = str(raised.value)
    assert str(path) in message
    assert "expected the last user message to contain" in message
    assert "a completely different request" in message


# Volatile identifiers: cut out of an expectation, not kept from matching only the run


UUID = "11111111-2222-3333-4444-555555555555"


def test_recording_cuts_a_uuid_out_of_the_expectation(tmp_path: Path) -> None:
    path = tmp_path / "cassette.json"
    provider = RecordingProvider(ScriptedModelProvider([{"response": {"reply": "ok"}}]), path)

    _ask(provider, f"Write service.restart, receipt {UUID}: landed, confirmed.")

    step = read_cassette(path)[0]
    assert step["expect_contains"] == "Write service.restart, receipt "
    assert UUID not in step["expect_contains"]


def test_a_uuid_alone_leaves_a_step_with_no_expectation(tmp_path: Path) -> None:
    path = tmp_path / "cassette.json"
    provider = RecordingProvider(ScriptedModelProvider([{"response": {"reply": "ok"}}]), path)

    _ask(provider, UUID)

    assert "expect_contains" not in read_cassette(path)[0]


def test_replay_cuts_a_stored_uuid_so_a_different_run_still_matches(tmp_path: Path) -> None:
    """A cassette recorded before the automatic cut still replays: the cut is applied on load."""
    other_run_uuid = "66666666-7777-8888-9999-000000000000"
    path = tmp_path / "cassette.json"
    path.write_text(
        json.dumps(
            [
                {
                    "purpose": "reply",
                    "expect_contains": f"receipt {UUID}: landed",
                    "response": {"reply": "ok"},
                }
            ]
        ),
        encoding="utf-8",
    )

    replay = ReplayProvider(path)

    assert _ask(replay, f"receipt {other_run_uuid}: landed, confirmed") == {"reply": "ok"}


def test_volatile_overrides_replace_the_default_patterns(tmp_path: Path) -> None:
    path = tmp_path / "cassette.json"
    provider = RecordingProvider(
        ScriptedModelProvider([{"response": {"reply": "ok"}}]), path, volatile=[r"case-\d+"]
    )

    _ask(provider, f"receipt {UUID} case-987654 closed")

    step = read_cassette(path)[0]
    # The default uuid patterns are replaced, not added to, so the uuid survives the cut.
    assert step["expect_contains"] == f"receipt {UUID} "
    assert "case-987654" not in step["expect_contains"]


def test_an_invalid_volatile_pattern_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "cassette.json"

    with pytest.raises(ModelProviderError, match="not a valid regular expression"):
        RecordingProvider(ScriptedModelProvider(SCRIPT), path, volatile=["("])


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


def test_a_script_mismatch_reaches_the_caller_as_a_script_mismatch_error(
    example_settings: Settings, scenario_dir: Path, tmp_path: Path
) -> None:
    """A mismatch is not a `ModelProviderError`, so nothing in the turn loop swallows it."""
    steps = json.loads(
        (scenario_dir / "02_restart_staging.json").read_text(encoding="utf-8")
    )["provider_script"]
    steps[0]["expect_contains"] = "a phrase this request will never hold"
    engine = build_engine(
        example_settings.model_copy(update={"state_dir": tmp_path / "state"}),
        provider=ScriptedModelProvider(steps),
    )

    with pytest.raises(ScriptMismatchError) as raised:
        engine.turn("mismatch", RESTART)

    assert not isinstance(raised.value, ModelProviderError)
    assert "expected the last user message to contain" in str(raised.value)


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


def test_run_refuses_an_invalid_volatile_pattern(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "run",
            "--text",
            RESTART,
            "--replay",
            str(tmp_path / "absent.json"),
            "--volatile",
            "(",
        ],
    )

    assert result.exit_code == 1
    assert "not a valid regular expression" in result.stderr
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


def test_cassette_to_script_volatile_replaces_the_defaults(tmp_path: Path) -> None:
    path = tmp_path / "cassette.json"
    path.write_text(
        json.dumps(
            [{"purpose": "reply", "expect_contains": "case-987654 closed", "response": {}}]
        ),
        encoding="utf-8",
    )

    result = runner.invoke(
        app, ["cassette", "to-script", str(path), "--volatile", r"case-\d+"]
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout) == [{"purpose": "reply", "response": {}}]


def test_cassette_to_script_refuses_an_invalid_volatile_pattern(tmp_path: Path) -> None:
    path = tmp_path / "cassette.json"
    path.write_text(json.dumps([{"purpose": "reply", "response": {}}]), encoding="utf-8")

    result = runner.invoke(app, ["cassette", "to-script", str(path), "--volatile", "("])

    assert result.exit_code == 1
    assert "not a valid regular expression" in result.stderr


class _NoVectors(ScriptedModelProvider):
    """A provider that answers and cannot embed."""

    embeds = False


def test_recording_and_replay_say_whether_the_session_embedded(tmp_path: Path) -> None:
    """A session recorded around a provider that cannot embed replays without vectors."""
    path = tmp_path / "cassette.json"
    recorder = RecordingProvider(_NoVectors(SCRIPT), path)
    recorder.complete_json("You answer.", [Message(role="user", content="restart it")], {})

    assert provider_embeds(recorder) is False
    assert provider_embeds(ReplayProvider(path)) is False

    embedding = tmp_path / "embedding.json"
    RecordingProvider(ScriptedModelProvider(SCRIPT), embedding).embed(["a text"])
    assert provider_embeds(ReplayProvider(embedding)) is True


ITALIAN_REQUEST = "Il servizio search in staging è bloccato, riavvialo per favore"


def _replay_turn(cassette: Path, text: str, *flags: str, session: str | None = "live") -> str:
    """One `oska run --replay` process: one turn, and the output it printed."""
    arguments = ["run", "--replay", str(cassette), "--text", text, *flags]
    if session is not None:
        arguments += ["--session", session]
    result = runner.invoke(app, arguments)
    assert result.exit_code == 0, result.stderr
    return result.stdout


def _replay_files(state: Path) -> list[Path]:
    return sorted((state / "replay").rglob("*.json")) if (state / "replay").exists() else []


@pytest.fixture
def italian_cassette(repo_root: Path) -> Path:
    return repo_root / "examples" / "ops" / "cassettes" / "restart-in-italian.json"


def test_a_replayed_session_goes_on_across_commands(
    cli_env: Path, italian_cassette: Path
) -> None:
    asked = _replay_turn(italian_cassette, ITALIAN_REQUEST)
    approved = _replay_turn(italian_cassette, "sì")
    again = _replay_turn(italian_cassette, ITALIAN_REQUEST)

    assert "approval_pending" in asked
    assert "receipt  integration.container_runtime service.restart" in approved
    assert "ledger_hit" in again
    (stored,) = _replay_files(cli_env)
    assert json.loads(stored.read_text(encoding="utf-8")) == {"position": 4}


def test_replay_from_start_ignores_the_stored_position(
    cli_env: Path, italian_cassette: Path
) -> None:
    progress = ReplayProgress.open(load_settings(), italian_cassette, "live", False)
    assert progress is not None
    progress.save(2)
    arguments = ["run", "--replay", str(italian_cassette), "--text", ITALIAN_REQUEST]

    resumed = runner.invoke(app, [*arguments, "--session", "live"])
    restarted = runner.invoke(app, [*arguments, "--session", "live", "--replay-from-start"])

    assert resumed.exit_code == 1
    assert "no longer match" in resumed.stderr
    assert restarted.exit_code == 0, restarted.stderr
    assert "approval_pending" in restarted.stdout
    assert json.loads(progress.path.read_text(encoding="utf-8")) == {"position": 1}


def test_a_changed_cassette_does_not_reuse_stored_progress(
    cli_env: Path, italian_cassette: Path, tmp_path: Path
) -> None:
    _replay_turn(italian_cassette, ITALIAN_REQUEST)
    changed = tmp_path / "changed.json"
    steps = json.loads(italian_cassette.read_text(encoding="utf-8"))
    changed.write_text(json.dumps(steps, indent=4), encoding="utf-8")
    settings = load_settings()

    unchanged = ReplayProgress.open(settings, italian_cassette, "live", False)
    rewritten = ReplayProgress.open(settings, changed, "live", False)

    assert unchanged is not None and unchanged.start == 1
    assert rewritten is not None and rewritten.start == 0
    assert unchanged.path.parent != rewritten.path.parent


def test_replay_without_a_session_stores_nothing(cli_env: Path, italian_cassette: Path) -> None:
    _replay_turn(italian_cassette, ITALIAN_REQUEST, session=None)

    assert _replay_files(cli_env) == []


def test_replay_that_runs_out_says_how_to_start_over(
    cli_env: Path, italian_cassette: Path
) -> None:
    _replay_turn(italian_cassette, ITALIAN_REQUEST)
    _replay_turn(italian_cassette, "sì")
    _replay_turn(italian_cassette, ITALIAN_REQUEST)

    result = runner.invoke(
        app, ["run", "--replay", str(italian_cassette), "--text", "ancora", "--session", "live"]
    )

    assert result.exit_code == 1
    assert "--replay-from-start" in result.stderr
    assert str(italian_cassette) in result.stderr


def test_replay_provider_resumes_from_a_start_and_reports_its_position(tmp_path: Path) -> None:
    path = tmp_path / "cassette.json"
    recorder = RecordingProvider(ScriptedModelProvider(SCRIPT), path)
    _ask(recorder, RESTART)
    _ask(recorder, "second question")

    replay = ReplayProvider(path, start=1)

    assert replay.position == 1
    assert _ask(replay, "second question") == _ask(
        ReplayProvider(path, start=1), "second question"
    )
    assert replay.position == 2
    with pytest.raises(ValueError, match="negative"):
        ReplayProvider(path, start=-1)
