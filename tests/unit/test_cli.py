"""The command line surface."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from click.testing import Result
from typer.testing import CliRunner

from oska.cli.main import MASK, app

runner = CliRunner()


def test_ontology_summary_names_entities_states_and_operations(skills_root: Path) -> None:
    result = runner.invoke(app, ["ontology", "--skills", str(skills_root), "--format", "summary"])
    assert result.exit_code == 0
    assert "entity.service" in result.stdout
    assert "identifier: name" in result.stdout
    assert "status: running, degraded, stopped" in result.stdout
    assert "relation deployments: many entity.deployment" in result.stdout
    assert "write integration.container_runtime service.restart -> status=running" in result.stdout
    assert "read integration.container_runtime service.status" in result.stdout


def test_ontology_json_is_the_graph(skills_root: Path) -> None:
    result = runner.invoke(app, ["ontology", "--skills", str(skills_root), "--format", "json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert {node["id"] for node in payload["nodes"] if node["kind"] == "entity"} == {
        "entity.alert",
        "entity.deployment",
        "entity.incident",
        "entity.service",
    }
    assert any(edge["kind"] == "relates_to" for edge in payload["edges"])


def test_ontology_mermaid_is_a_diagram(skills_root: Path) -> None:
    result = runner.invoke(app, ["ontology", "--skills", str(skills_root), "--format", "mermaid"])
    assert result.exit_code == 0
    assert result.stdout.startswith("graph LR")
    assert "subgraph Entities" in result.stdout


def test_ontology_of_an_empty_catalog_prints_nothing(tmp_path: Path) -> None:
    result = runner.invoke(app, ["ontology", "--skills", str(tmp_path / "absent")])
    assert result.exit_code == 0
    assert result.stdout.strip() == ""


@pytest.fixture
def cli_env(
    monkeypatch: pytest.MonkeyPatch, repo_root: Path, tmp_path: Path
) -> Path:
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


@pytest.fixture
def scenario_dir(repo_root: Path) -> Path:
    """The example scenario directory."""
    return repo_root / "examples" / "ops" / "scenarios"


def _run_session(scenario: Path, session: str, turns: list[str]) -> Result:
    """Drive the read loop of one session with the script of one scenario."""
    return runner.invoke(
        app,
        ["run", "--session", session, "--provider", "scripted", "--script", str(scenario)],
        input="".join(f"{turn}\n" for turn in turns) + "/quit\n",
    )


def test_run_one_turn_answers_from_the_catalog(cli_env: Path, scenario_dir: Path) -> None:
    result = runner.invoke(
        app,
        [
            "run",
            "--session",
            "greeting",
            "--text",
            "hello",
            "--provider",
            "scripted",
            "--script",
            str(scenario_dir / "01_greeting.json"),
        ],
    )

    assert result.exit_code == 0
    assert "restart a service" in result.stdout


def test_the_scripted_provider_needs_a_script(cli_env: Path) -> None:
    result = runner.invoke(app, ["run", "--text", "hello", "--provider", "scripted"])

    assert result.exit_code == 1
    assert "--script" in result.stderr
    assert "Traceback" not in result.stderr


def test_run_prints_the_payload_held_at_the_gate_then_the_receipt(
    cli_env: Path, scenario_dir: Path
) -> None:
    result = _run_session(
        scenario_dir / "02_restart_staging.json",
        "restart",
        ["the search service in staging needs a restart, it is wedged again", "yes"],
    )

    assert result.exit_code == 0
    assert "awaiting approval" in result.stdout
    assert 'integration.container_runtime service.restart {"service":"search"}' in result.stdout
    assert "approvals 0 of 1" in result.stdout
    assert "approval_pending" in result.stdout
    assert "receipt  integration.container_runtime service.restart" in result.stdout


def test_receipts_read_back_what_the_session_wrote(cli_env: Path, scenario_dir: Path) -> None:
    _run_session(
        scenario_dir / "02_restart_staging.json",
        "restart",
        ["the search service in staging needs a restart, it is wedged again", "yes"],
    )

    result = runner.invoke(app, ["receipts", "--session", "restart", "--json"])

    assert result.exit_code == 0
    receipts = json.loads(result.stdout)
    assert len(receipts) == 1
    assert receipts[0]["operation"] == "service.restart"
    assert receipts[0]["payload"] == {"service": "search"}
    assert receipts[0]["status"] == "executed"
    assert receipts[0]["verification"] == "verified"


def test_receipts_of_a_session_that_wrote_nothing_are_empty(cli_env: Path) -> None:
    result = runner.invoke(app, ["receipts", "--session", "never-ran", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == []


def test_a_proposal_is_drafted_listed_shown_and_moved(
    cli_env: Path, scenario_dir: Path
) -> None:
    _run_session(
        scenario_dir / "02_restart_staging.json",
        "restart",
        ["the search service in staging needs a restart, it is wedged again", "yes"],
    )

    drafted = runner.invoke(app, ["propose", "from-session", "restart", "--by", "s.reyes"])
    assert drafted.exit_code == 0
    proposal_id = drafted.stdout.split()[0]
    assert "draft" in drafted.stdout

    listed = runner.invoke(app, ["propose", "list", "--state", "draft"])
    assert listed.exit_code == 0
    assert proposal_id in listed.stdout

    shown = runner.invoke(app, ["propose", "show", proposal_id])
    assert shown.exit_code == 0
    document = json.loads(shown.stdout)
    assert document["proposal_id"] == proposal_id
    assert document["source_session_id"] == "restart"
    assert document["history"][0]["by"] == "s.reyes"

    moved = runner.invoke(
        app,
        ["propose", "transition", proposal_id, "--to", "submitted", "--by", "s.reyes"],
    )
    assert moved.exit_code == 0
    assert "submitted" in moved.stdout

    assert (cli_env / "proposals" / f"{proposal_id}.json").is_file()


def test_an_edge_the_lifecycle_refuses_leaves_one_line(
    cli_env: Path, scenario_dir: Path
) -> None:
    _run_session(
        scenario_dir / "02_restart_staging.json",
        "restart",
        ["the search service in staging needs a restart, it is wedged again", "yes"],
    )
    drafted = runner.invoke(app, ["propose", "from-session", "restart"])
    proposal_id = drafted.stdout.split()[0]

    result = runner.invoke(
        app,
        ["propose", "transition", proposal_id, "--to", "published", "--by", "s.reyes"],
    )

    assert result.exit_code == 1
    assert "draft does not lead to published" in result.stderr
    assert "Traceback" not in result.stderr


def test_a_transition_without_an_actor_is_refused(cli_env: Path, scenario_dir: Path) -> None:
    _run_session(
        scenario_dir / "02_restart_staging.json",
        "restart",
        ["the search service in staging needs a restart, it is wedged again", "yes"],
    )
    drafted = runner.invoke(app, ["propose", "from-session", "restart"])
    proposal_id = drafted.stdout.split()[0]

    result = runner.invoke(
        app, ["propose", "transition", proposal_id, "--to", "submitted", "--by", " "]
    )

    assert result.exit_code == 1
    assert "actor" in result.stderr


def test_a_catalog_that_carries_an_error_stops_a_run(
    cli_env: Path,
    tmp_path: Path,
    write_skill: Callable[..., Path],
    runbook_frontmatter: Callable[..., dict],
) -> None:
    write_skill(runbook_frontmatter(integrations=["integration.absent"]))

    result = runner.invoke(
        app, ["run", "--skills", str(tmp_path / "skills"), "--text", "hello"]
    )

    assert result.exit_code == 1
    assert "the catalog does not hold up" in result.stderr
    assert "unknown_reference" in result.stderr
    assert "Traceback" not in result.stderr


def test_config_prints_the_settings_in_force(repo_root: Path, tmp_path: Path) -> None:
    state = tmp_path / "elsewhere"
    environment = {
        "OSKA_SKILLS_ROOTS": str(repo_root / "examples" / "ops" / "skills"),
        "OSKA_RUNTIME_CONFIG": str(repo_root / "configs" / "runtime.json"),
        "OSKA_MODEL_CONFIG": str(repo_root / "configs" / "model.json"),
        "OSKA_STATE_DIR": str(state),
        "OSKA_MODEL_CHAT": "a-model-the-environment-names",
        "OSKA_MODEL_API_KEY": "not-to-be-printed",
    }

    result = CliRunner(env=environment).invoke(app, ["config"])

    assert result.exit_code == 0
    document = json.loads(result.stdout)
    assert document["settings"]["state_dir"] == str(state)
    assert document["model"]["chat_model"] == "a-model-the-environment-names"
    assert document["model"]["api_key"] == MASK
    assert "not-to-be-printed" not in result.stdout
    assert document["runtime"]["discovery"]["tie_margin"] == 0.05
    assert "yes" in document["runtime"]["router"]["rules"]["approve_tokens"]


def test_conform_reports_the_adaptive_level(cli_env: Path) -> None:
    result = runner.invoke(app, ["conform", "--json"])

    assert result.exit_code == 0
    report = json.loads(result.stdout)
    assert report["passed"] is True
    assert report["level"] == 3
    assert report["by_level"]["3"] == {"passed": 1, "total": 1}


def test_conform_as_text_names_every_scenario(cli_env: Path, scenario_dir: Path) -> None:
    result = runner.invoke(app, ["conform", "--scenarios", str(scenario_dir)])

    assert result.exit_code == 0
    assert "conformance level 3" in result.stdout
    assert "15_learning_lifecycle" in result.stdout


def test_conform_on_a_directory_that_is_not_there(cli_env: Path, tmp_path: Path) -> None:
    result = runner.invoke(app, ["conform", "--scenarios", str(tmp_path / "absent")])

    assert result.exit_code == 1
    assert "no scenario directory" in result.stderr


def test_serve_declares_its_host_and_port(cli_env: Path) -> None:
    result = runner.invoke(app, ["serve", "--help"])

    assert result.exit_code == 0
    assert "--host" in result.stdout
    assert "--port" in result.stdout


def test_serve_listens_where_the_configuration_says(
    cli_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import uvicorn

    served: dict = {}

    def _record(application: object, **options: object) -> None:
        served["application"] = application
        served.update(options)

    monkeypatch.setattr(uvicorn, "run", _record)

    assert runner.invoke(app, ["serve"]).exit_code == 0
    assert served["host"] == "127.0.0.1"
    assert served["port"] == 8000

    assert runner.invoke(app, ["serve", "--port", "8123"]).exit_code == 0
    assert served["port"] == 8123
