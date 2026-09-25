"""Conformance of a platform, domain by domain.

Every scenario of every pack a domain serves runs against a fresh engine built
the way that domain builds its own: the domain's catalog, bindings, guardrails
and host, the tenant's runtime fragment, and a state directory of its own. The
level is reported per domain, because a domain is what a person talks to.

Each scenario keeps its record under a namespace the run creates, with the
(tenant, domain) nested inside it, so pairs stay apart as they do when served,
and the run purges it when the scenario ends; see `oska.conformance.runner`.
"""

from __future__ import annotations

from oska.conformance.report import attained_level, render_json, render_text
from oska.conformance.runner import SuiteReport, run_directories
from oska.platform.runtime import Platform

__all__ = ["passed", "render_platform_json", "render_platform_text", "run_platform"]


def run_platform(platform: Platform, tenant: str | None = None) -> dict[str, SuiteReport]:
    """Run every domain's scenarios with that domain's engine, one report per domain."""
    reports: dict[str, SuiteReport] = {}
    for spec in platform.domains():
        chosen = platform.tenant(spec.name, tenant)
        directories = [pack.scenarios for pack in platform.packs(spec.name)]
        reports[spec.name] = SuiteReport.of(
            run_directories(
                [directory for directory in directories if directory.is_dir()],
                platform.settings_for(spec.name, chosen),
                platform.builder(spec.name, chosen),
            )
        )
    return reports


def passed(platform: Platform, reports: dict[str, SuiteReport]) -> bool:
    """Whether every domain passed and reached the level the platform targets."""
    target = platform.manifest.target_level
    return all(
        report.passed and attained_level(report) >= target for report in reports.values()
    )


def render_platform_text(platform: Platform, reports: dict[str, SuiteReport]) -> str:
    """Each domain's report under its name, then the level of each against the target."""
    lines: list[str] = []
    for name, report in reports.items():
        lines.extend([f"domain {name}", render_text(report), ""])
    target = platform.manifest.target_level
    for name, report in reports.items():
        lines.append(f"domain {name}: conformance level {attained_level(report)}")
    lines.append(f"platform {platform.manifest.name}: target level {target}")
    return "\n".join(lines)


def render_platform_json(platform: Platform, reports: dict[str, SuiteReport]) -> dict:
    """The same reports as plain data, keyed by domain."""
    return {
        "platform": platform.manifest.name,
        "target_level": platform.manifest.target_level,
        "passed": passed(platform, reports),
        "domains": {name: render_json(report) for name, report in reports.items()},
    }
