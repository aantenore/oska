"""The conformance runner, over the example scenario directory."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from oska.config import Settings
from oska.conformance import (
    ScenarioFile,
    SuiteReport,
    attained_level,
    load_scenarios,
    render_json,
    render_text,
    run_scenario,
    run_suite,
)
from oska.conformance.runner import DEFAULT_LEVEL

ABSENT = "no reply says anything remotely like this"
ADAPTIVE = 3
LEARNING = "15_learning_lifecycle"


@pytest.fixture
def scenario_dir(repo_root: Path) -> Path:
    """The example scenario directory."""
    return repo_root / "examples" / "ops" / "scenarios"


@pytest.fixture
def scenarios(scenario_dir: Path) -> list[ScenarioFile]:
    """Every example scenario, in file name order."""
    return load_scenarios(scenario_dir)


def test_load_scenarios_reads_the_example_directory(scenarios: list[ScenarioFile]) -> None:
    assert scenarios
    assert [scenario.name for scenario in scenarios] == sorted(
        scenario.name for scenario in scenarios
    )
    for scenario in scenarios:
        assert scenario.description
        assert scenario.level >= 1
        assert scenario.turns
        for turn in scenario.turns:
            assert turn.user
            assert turn.expect.kind


def test_a_scenario_without_a_level_is_read_as_governed() -> None:
    scenario = ScenarioFile.model_validate(
        {"name": "unstamped", "turns": [{"user": "hello", "expect": {"kind": "greeting"}}]}
    )
    assert scenario.level == DEFAULT_LEVEL
    assert scenario.learning is None


def test_the_examples_stamp_every_level(scenarios: list[ScenarioFile]) -> None:
    levels = {scenario.name: scenario.level for scenario in scenarios}
    assert levels["01_greeting"] == 1
    assert levels["08_off_scope"] == 1
    assert levels["02_restart_staging"] == DEFAULT_LEVEL
    assert levels[LEARNING] == ADAPTIVE
    assert set(levels.values()) == {1, DEFAULT_LEVEL, ADAPTIVE}


def test_the_examples_write_behaviors_at_the_turn_level(
    scenarios: list[ScenarioFile], scenario_dir: Path
) -> None:
    behaviors = [
        turn.connector_behaviors
        for scenario in scenarios
        for turn in scenario.turns
        if turn.connector_behaviors
    ]
    assert behaviors, "no example scenario injects a connector behavior"
    for path in sorted(scenario_dir.glob("*.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        for turn in document["turns"]:
            assert "connector_behaviors" not in turn["expect"], path.name


def test_suite_passes_and_reports_every_level(
    scenario_dir: Path, example_settings: Settings
) -> None:
    report = run_suite(scenario_dir, example_settings)
    assert report.passed, render_text(report)
    assert len(report.results) == len(load_scenarios(scenario_dir))
    assert report.by_level
    for passed, total in report.by_level.values():
        assert passed == total
    assert sum(total for _, total in report.by_level.values()) == len(report.results)


def test_render_text_names_the_level(scenario_dir: Path, example_settings: Settings) -> None:
    report = run_suite(scenario_dir, example_settings)
    text = render_text(report)
    assert f"conformance level {ADAPTIVE}" in text
    assert attained_level(report) == ADAPTIVE
    for level in (1, DEFAULT_LEVEL, ADAPTIVE):
        passed, total = report.by_level[level]
        assert f"level {level}: {passed}/{total} scenarios pass" in text
    for result in report.results:
        assert result.name in text


def test_render_json_carries_the_same_numbers(
    scenario_dir: Path, example_settings: Settings
) -> None:
    report = run_suite(scenario_dir, example_settings)
    document = render_json(report)
    assert document["passed"] is True
    assert document["level"] == ADAPTIVE
    assert len(document["results"]) == len(report.results)
    assert sum(entry["total"] for entry in document["by_level"].values()) == len(report.results)
    assert document["by_level"][str(ADAPTIVE)] == {"passed": 1, "total": 1}


def test_a_wrong_expectation_fails_readably(
    scenarios: list[ScenarioFile], example_settings: Settings, tmp_path: Path
) -> None:
    original = scenarios[0]
    broken = original.model_copy(deep=True)
    broken.turns[0].expect.contains = [ABSENT]

    result = run_scenario(broken, example_settings, state_dir=tmp_path / "broken")

    assert result.passed is False
    assert result.turns == len(broken.turns)
    assert any(ABSENT in failure for failure in result.failures)
    assert any(failure.startswith("turn 1:") for failure in result.failures)

    text = render_text(SuiteReport.of([result]))
    assert text.startswith("fail  ")
    assert ABSENT in text
    assert "conformance level 0" in text


@pytest.fixture
def learning_scenario(scenarios: list[ScenarioFile]) -> ScenarioFile:
    """The adaptive scenario: a session that becomes a published procedure."""
    return next(scenario for scenario in scenarios if scenario.name == LEARNING)


def test_the_learning_block_is_read_from_the_file(learning_scenario: ScenarioFile) -> None:
    spec = learning_scenario.learning
    assert spec is not None
    assert spec.draft_from_session is True
    assert [step.to for step in spec.transitions] == ["submitted", "approved", "published"]
    assert {step.by for step in spec.transitions} == {"s.reyes"}
    assert spec.expect.state == "published"
    assert spec.expect.published_id == "runbook.service_restart"
    assert spec.expect.quality_passed is True
    assert spec.expect_refused is not None
    assert spec.expect_refused.by == ""
    assert spec.provider_script


def test_the_adaptive_scenario_publishes_into_the_catalog(
    learning_scenario: ScenarioFile, example_settings: Settings, tmp_path: Path
) -> None:
    result = run_scenario(learning_scenario, example_settings, state_dir=tmp_path / "state")

    assert result.passed, "\n".join(result.failures)
    assert result.level == ADAPTIVE
    published = tmp_path / "state" / "skills" / "runbook" / "service_restart" / "v2"
    assert (published / "skill.md").is_file()
    assert (published / "policy.json").is_file()
    proposals = list((tmp_path / "state" / "proposals").glob("*.json"))
    assert len(proposals) == 1
    assert json.loads(proposals[0].read_text(encoding="utf-8"))["state"] == "published"


def test_a_transition_that_should_be_refused_but_is_not_fails(
    learning_scenario: ScenarioFile, example_settings: Settings, tmp_path: Path
) -> None:
    broken = learning_scenario.model_copy(deep=True)
    assert broken.learning is not None
    broken.learning.expect_refused.by = "s.reyes"

    result = run_scenario(broken, example_settings, state_dir=tmp_path / "state")

    assert result.passed is False
    assert any("expects it to be refused" in failure for failure in result.failures)


def test_a_publication_that_never_reaches_the_catalog_fails(
    learning_scenario: ScenarioFile, example_settings: Settings, tmp_path: Path
) -> None:
    broken = learning_scenario.model_copy(deep=True)
    assert broken.learning is not None
    broken.learning.transitions = broken.learning.transitions[:2]

    result = run_scenario(broken, example_settings, state_dir=tmp_path / "state")

    assert result.passed is False
    assert any("the scenario expects 'published'" in failure for failure in result.failures)
    assert any("nothing was picked up from the publication root" in f for f in result.failures)
