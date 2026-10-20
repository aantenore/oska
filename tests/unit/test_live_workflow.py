"""What the manual live-model workflow does, read from its own file.

`.github/workflows/live-model.yml` runs a short session against a real model
endpoint and records it. It spends a key, so this holds the file to the rules
that keep the spending deliberate and the key out of every log: it starts only
by hand, the secret and the inputs reach the shell through `env:` and never
through the script text, and the cassette is checked for the key before it is
uploaded.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "live-model.yml"
SECRET = "OSKA_MODEL_API_KEY"
INPUTS = ("provider", "base_url", "chat_model", "embedding_model", "record")


def _document() -> dict[Any, Any]:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _triggers() -> dict[str, Any]:
    document = _document()
    # YAML 1.1 reads an unquoted `on` as the boolean True.
    return document["on"] if "on" in document else document[True]


def _job() -> dict[str, Any]:
    jobs = _document()["jobs"]
    assert len(jobs) == 1
    return next(iter(jobs.values()))


def _steps() -> list[dict[str, Any]]:
    return _job()["steps"]


def _scripts() -> list[str]:
    return [step["run"] for step in _steps() if "run" in step]


def test_the_workflow_starts_only_by_hand() -> None:
    assert set(_triggers()) == {"workflow_dispatch"}


def test_the_workflow_takes_the_inputs_the_session_needs() -> None:
    inputs = _triggers()["workflow_dispatch"]["inputs"]
    assert tuple(inputs) == INPUTS
    assert inputs["provider"]["options"] == ["openai_compatible", "anthropic"]
    assert inputs["record"]["type"] == "boolean" and inputs["record"]["default"] is False
    assert inputs["embedding_model"].get("required", False) is False
    assert inputs["base_url"]["required"] and inputs["chat_model"]["required"]


def test_the_secret_reaches_the_job_through_env_and_no_script() -> None:
    assert _job()["env"][SECRET] == f"${{{{ secrets.{SECRET} }}}}"
    text = WORKFLOW.read_text(encoding="utf-8")
    assert text.count("secrets.") == 1
    for script in _scripts():
        assert "secrets." not in script and "${{" not in script
    for step in _steps():
        assert "secrets." not in str(step.get("with", {}))


def test_no_script_interpolates_an_expression() -> None:
    """Inputs and the rest of the context reach a script as variables, not as text."""
    for script in _scripts():
        assert "${{" not in script, script
        assert "inputs." not in script and "github.event" not in script


def test_the_inputs_are_passed_through_env() -> None:
    env = _job()["env"]
    for name in ("base_url", "chat_model", "embedding_model", "provider"):
        assert any(value == f"${{{{ inputs.{name} }}}}" for value in env.values()), name
    assert {"OSKA_MODEL_BASE_URL", "OSKA_MODEL_CHAT", "OSKA_MODEL_EMBEDDING"} <= set(env)


def test_an_empty_secret_ends_the_job_without_failing() -> None:
    steps = _steps()
    check = steps[0]
    assert check["id"] == "key"
    assert f'[ -z "${SECRET}" ]' in check["run"] and "::notice::" in check["run"]
    assert "exit 1" not in check["run"]
    for step in steps[1:]:
        gate = step["if"]
        # The upload follows the scan, which is itself skipped when there is no key.
        assert "steps.key.outputs.present == 'true'" in gate or "steps.scan.outcome" in gate, step


def test_the_session_is_recorded_across_three_runs_of_the_same_session() -> None:
    session = next(script for script in _scripts() if "oska run" in script)
    assert session.count("oska run") == 1, "one helper runs every turn"
    assert "--record" in session and "live.json" in session
    assert '--provider "$OSKA_PROVIDER"' in session and '--session "$LIVE_SESSION"' in session
    assert session.count('turn "') == 3
    assert "approval_pending" in session
    assert "'^receipt  '" in session and "verified" in session
    assert "'^ledger_hit  '" in session


def test_the_cassette_is_checked_for_the_key_before_it_is_uploaded() -> None:
    steps = _steps()
    scan = next(i for i, step in enumerate(steps) if step.get("id") == "scan")
    upload = next(i for i, step in enumerate(steps) if "upload-artifact" in step.get("uses", ""))
    assert scan < upload
    script = steps[scan]["run"]
    assert "grep -qF" in script and SECRET in script and "exit 1" in script
    assert "echo \"$OSKA_MODEL_API_KEY\"" not in script, "the key is never printed"
    assert "steps.scan.outcome == 'success'" in steps[upload]["if"]


def test_the_recorded_cassette_is_uploaded_as_an_artifact() -> None:
    uploads = [step for step in _steps() if "upload-artifact" in step.get("uses", "")]
    assert len(uploads) == 1
    assert uploads[0]["with"]["path"].endswith("/live.json")
    assert uploads[0]["with"]["name"] == "live-cassette"


def test_the_key_is_never_echoed() -> None:
    for script in _scripts():
        for line in script.splitlines():
            if line.strip().startswith(("echo", "printf")):
                assert SECRET not in line or "::notice::" in line or "::error::" in line, line
