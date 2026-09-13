"""Rendering a suite report, as text for a terminal and as data for a caller."""

from __future__ import annotations

from opsdeck.conformance.runner import ScenarioResult, SuiteReport

__all__ = ["attained_level", "render_json", "render_text"]

PASS = "pass"
FAIL = "fail"
NO_LEVEL = 0


def attained_level(report: SuiteReport) -> int:
    """The highest level whose scenarios, and those of every level below, all pass."""
    attained = NO_LEVEL
    for level in sorted(report.by_level):
        passed, total = report.by_level[level]
        if passed != total:
            break
        attained = level
    return attained


def render_text(report: SuiteReport) -> str:
    """One line per scenario, then the totals per level, then the level attained."""
    lines = [_scenario_line(result) for result in report.results]
    for failure in (line for result in report.results for line in _failure_lines(result)):
        lines.append(failure)
    if not lines:
        lines.append("no scenario was run")
    lines.append("")
    for level in sorted(report.by_level):
        passed, total = report.by_level[level]
        lines.append(f"level {level}: {passed}/{total} scenarios pass")
    lines.append(f"conformance level {attained_level(report)}")
    return "\n".join(lines)


def render_json(report: SuiteReport) -> dict:
    """The same report as plain data."""
    return {
        "passed": report.passed,
        "level": attained_level(report),
        "by_level": {
            str(level): {"passed": passed, "total": total}
            for level, (passed, total) in sorted(report.by_level.items())
        },
        "results": [result.model_dump(mode="json") for result in report.results],
    }


def _scenario_line(result: ScenarioResult) -> str:
    outcome = PASS if result.passed else FAIL
    return f"{outcome:>4}  {result.name}  level {result.level}  {result.turns} turn(s)"


def _failure_lines(result: ScenarioResult) -> list[str]:
    return [f"      {result.name}: {failure}" for failure in result.failures]
