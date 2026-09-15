"""The evaluation set a skill carries, and what running the whole catalog proves."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import CliRunner

from opsdeck.catalog.evaluation import (
    EVALUATION_FILENAME,
    EvaluationCase,
    EvaluationSetError,
    cases_from_neighbors,
    evaluate_catalog,
    evaluation_path,
    load_cases,
    run_cases,
)
from opsdeck.catalog.registry import SkillRegistry
from opsdeck.cli.commands_evaluate import app
from opsdeck.config import Settings, load_runtime_config
from opsdeck.index.discovery import Discovery
from opsdeck.index.keyword import KeywordIndex
from opsdeck.learning import quality

runner = CliRunner()

STOLEN = "please restart the payments service"


def _discovery(registry: SkillRegistry, repo_root: Path) -> Discovery:
    """The ranking the runtime serves with no model provider configured."""
    runtime = load_runtime_config(
        settings=Settings(runtime_config=repo_root / "configs" / "runtime.json")
    )
    return Discovery(registry, KeywordIndex(), runtime.discovery, runtime.enrichment)


@pytest.fixture
def ops_registry(skills_root: Path) -> SkillRegistry:
    """Registry over the operational catalog."""
    return SkillRegistry([skills_root])


@pytest.fixture
def access_registry(repo_root: Path) -> SkillRegistry:
    """Registry over the access catalog."""
    return SkillRegistry([repo_root / "examples" / "access" / "skills"])


def test_a_skill_without_a_set_reads_as_no_cases(ops_registry: SkillRegistry) -> None:
    assert load_cases(ops_registry.require("runbook.service_health")) == []


def test_the_set_lives_beside_the_skill_in_its_version_directory(
    ops_registry: SkillRegistry,
) -> None:
    skill = ops_registry.require("runbook.service_restart")
    path = evaluation_path(skill)

    assert path == Path(skill.path).parent / EVALUATION_FILENAME
    assert path.is_file()


def test_the_declared_cases_say_what_must_reach_the_skill_and_what_must_not(
    ops_registry: SkillRegistry,
) -> None:
    cases = load_cases(ops_registry.require("runbook.service_restart"))

    assert [case.expects for case in cases].count("this") >= 1
    assert [case.expects for case in cases].count("not this") >= 2
    assert {case.language for case in cases} >= {"en", "it"}


def test_a_set_that_cannot_be_read_is_an_error_and_not_an_empty_set(
    catalog_copy: Path,
) -> None:
    registry = SkillRegistry([catalog_copy])
    skill = registry.require("runbook.service_restart")
    evaluation_path(skill).write_text("{ not json", encoding="utf-8")

    with pytest.raises(EvaluationSetError):
        load_cases(skill)


def test_a_set_that_is_not_a_list_of_cases_is_an_error(catalog_copy: Path) -> None:
    registry = SkillRegistry([catalog_copy])
    skill = registry.require("runbook.service_restart")
    evaluation_path(skill).write_text(json.dumps({"cases": {}}), encoding="utf-8")

    with pytest.raises(EvaluationSetError):
        load_cases(skill)


def test_the_operational_catalog_answers_every_case_it_declares(
    ops_registry: SkillRegistry, repo_root: Path
) -> None:
    report = evaluate_catalog(ops_registry, _discovery(ops_registry, repo_root))

    assert report.failures == []
    assert report.precision == 1.0
    assert {entry.skill_id for entry in report.per_skill} == {
        "runbook.alert_silence",
        "runbook.log_search",
        "runbook.service_restart",
    }


def test_the_access_catalog_answers_every_case_it_declares(
    access_registry: SkillRegistry, repo_root: Path
) -> None:
    report = evaluate_catalog(access_registry, _discovery(access_registry, repo_root))

    assert report.failures == []
    assert report.precision == 1.0
    assert {entry.skill_id for entry in report.per_skill} == {
        "runbook.access_grant",
        "runbook.access_revoke",
    }


def test_a_new_skill_that_steals_another_s_requests_fails_the_evaluation(
    catalog_copy: Path,
    repo_root: Path,
    write_skill: Callable[..., Path],
    runbook_frontmatter: Callable[..., dict],
) -> None:
    write_skill(
        runbook_frontmatter(
            id="runbook.service_reboot",
            description="Reboots the host a service runs on, which is not a restart of it.",
            triggers=[STOLEN, "restart the payments service", "restart payments"],
            entities=["entity.service"],
        ),
        root=catalog_copy,
    )
    registry = SkillRegistry([catalog_copy])

    report = evaluate_catalog(registry, _discovery(registry, repo_root))

    stolen = [result for result in report.failures if result.text == STOLEN]
    assert stolen and stolen[0].skill_id == "runbook.service_restart"
    assert report.precision < 1.0
    assert not report.ok


def test_a_case_that_ranks_first_below_the_floor_does_not_pass(
    ops_registry: SkillRegistry, repo_root: Path
) -> None:
    results = run_cases(
        [EvaluationCase(text="zzz", expects="this")],
        "runbook.service_restart",
        _discovery(ops_registry, repo_root),
    )

    assert not results[0].passed
    assert results[0].chosen is None


def test_proposed_cases_are_drawn_from_the_skills_that_rank_closest(
    ops_registry: SkillRegistry, repo_root: Path
) -> None:
    skill = ops_registry.require("runbook.service_health")

    proposed = cases_from_neighbors(skill, ops_registry, _discovery(ops_registry, repo_root))

    assert proposed
    assert {case.expects for case in proposed} == {"not this"}
    declared = {trigger for trigger in skill.frontmatter.triggers}
    assert not {case.text for case in proposed} & declared


def test_proposing_cases_writes_nothing_beside_the_skill(
    ops_registry: SkillRegistry, repo_root: Path
) -> None:
    skill = ops_registry.require("runbook.service_health")

    cases_from_neighbors(skill, ops_registry, _discovery(ops_registry, repo_root))

    assert not evaluation_path(skill).exists()


def test_without_a_ranking_the_neighbors_are_the_skills_touching_the_same_things(
    ops_registry: SkillRegistry,
) -> None:
    skill = ops_registry.require("runbook.service_health")

    proposed = cases_from_neighbors(skill, ops_registry)

    assert proposed
    assert {case.expects for case in proposed} == {"not this"}


def test_the_quality_gate_refuses_a_revision_that_fails_its_own_cases(
    catalog_copy: Path,
    repo_root: Path,
    write_skill: Callable[..., Path],
    runbook_frontmatter: Callable[..., dict],
) -> None:
    path = _revised_away_from_its_cases(catalog_copy, write_skill, runbook_frontmatter)
    markdown = path.read_text(encoding="utf-8")
    registry = SkillRegistry([catalog_copy])

    report = quality.check(
        markdown,
        registry,
        schema_dir=repo_root / "schemas",
        ranking=_discovery(registry, repo_root),
    )

    assert any(issue.startswith("evaluation:") for issue in report.issues)
    assert not quality.passed(report)


def test_the_quality_gate_does_not_refuse_a_skill_that_carries_no_set(
    catalog_copy: Path, repo_root: Path
) -> None:
    path = catalog_copy / "runbook" / "service_health" / "v1" / "skill.md"
    registry = SkillRegistry([catalog_copy])

    report = quality.check(
        path.read_text(encoding="utf-8"),
        registry,
        schema_dir=repo_root / "schemas",
        ranking=_discovery(registry, repo_root),
    )

    assert not [issue for issue in report.issues if issue.startswith("evaluation:")]


def test_the_quality_gate_without_a_ranking_runs_no_cases(
    catalog_copy: Path,
    repo_root: Path,
    write_skill: Callable[..., Path],
    runbook_frontmatter: Callable[..., dict],
) -> None:
    path = _revised_away_from_its_cases(catalog_copy, write_skill, runbook_frontmatter)

    report = quality.check(
        path.read_text(encoding="utf-8"),
        SkillRegistry([catalog_copy]),
        schema_dir=repo_root / "schemas",
    )

    assert not [issue for issue in report.issues if issue.startswith("evaluation:")]


def _revised_away_from_its_cases(
    root: Path,
    write_skill: Callable[..., Path],
    runbook_frontmatter: Callable[..., dict],
) -> Path:
    """Rewrite a procedure so its own evaluation set no longer reaches it.

    The set is left where it is, beside the skill, which is what an author who
    rewrites the triggers and forgets the cases actually leaves behind.
    """
    return write_skill(
        runbook_frontmatter(
            id="runbook.service_restart",
            description="Counts the aardvarks held in the inventory, nothing like a restart.",
            triggers=[
                "catalog the aardvark inventory",
                "count the aardvarks we hold",
                "list the aardvark holdings",
            ],
        ),
        root=root,
    )


def test_the_command_passes_over_both_reference_catalogs(repo_root: Path) -> None:
    for catalog in ("ops", "access"):
        root = repo_root / "examples" / catalog / "skills"
        result = runner.invoke(app, ["catalog", "--skills", str(root)])

        assert result.exit_code == 0, result.stdout
        assert "precision 1.0" in result.stdout


def test_the_command_stops_with_a_non_zero_status_on_a_failing_case(
    catalog_copy: Path,
    write_skill: Callable[..., Path],
    runbook_frontmatter: Callable[..., dict],
) -> None:
    write_skill(
        runbook_frontmatter(
            id="runbook.service_reboot",
            description="Reboots the host a service runs on, which is not a restart of it.",
            triggers=[STOLEN, "restart the payments service", "restart payments"],
        ),
        root=catalog_copy,
    )

    result = runner.invoke(app, ["catalog", "--skills", str(catalog_copy)])

    assert result.exit_code == 1
    assert "failed: runbook.service_restart" in result.stdout


def test_the_command_reports_a_set_it_cannot_read(catalog_copy: Path) -> None:
    registry = SkillRegistry([catalog_copy])
    evaluation_path(registry.require("runbook.service_restart")).write_text(
        "{ not json", encoding="utf-8"
    )

    result = runner.invoke(app, ["catalog", "--skills", str(catalog_copy)])

    assert result.exit_code == 1


def test_the_command_suggests_cases_an_author_can_paste(skills_root: Path) -> None:
    result = runner.invoke(
        app, ["suggest", "runbook.service_health", "--skills", str(skills_root), "--json"]
    )

    assert result.exit_code == 0
    proposed = json.loads(result.stdout)["cases"]
    assert proposed and {case["expects"] for case in proposed} == {"not this"}


def test_suggesting_for_an_unknown_skill_fails(skills_root: Path) -> None:
    result = runner.invoke(app, ["suggest", "runbook.absent", "--skills", str(skills_root)])

    assert result.exit_code == 1
