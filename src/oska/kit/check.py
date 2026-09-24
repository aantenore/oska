"""Checking one pack in isolation: validate it, evaluate it, run its scenarios.

`check_pack` is what `oska new pack` promises when it says the pack it writes
passes immediately, and what a pack author runs after every change. It answers
one question a person cannot answer by reading the files: does this pack, run
on its own with its own connectors, reach the conformance level it claims.

The pack is run the way a platform serves it: as the one pack of a domain,
assembled by `oska.platform`, so its guardrails, synonyms and host reach the
engine its scenarios run on, its skills are validated against the guardrail
catalog that domain would hold, and the level it reports is the level a
platform will measure. The bindings are held to what a platform holds them to:
a mock fixture is read only from inside the pack and nothing is read from the
environment. A check runs the pack's bindings, and a pack may come from anyone,
so a `shell` binding, which runs a command on this machine, is refused unless
the caller allows it.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from oska.catalog.evaluation import CatalogEvaluation, EvaluationSetError, evaluate_catalog
from oska.catalog.registry import SkillRegistry
from oska.catalog.validator import (
    CatalogError,
    ValidationIssue,
    declared_guardrail_ids,
    validate,
)
from oska.config import (
    Settings,
    guardrail_catalog_path,
    load_runtime_config,
    load_settings,
    state_path,
)
from oska.conformance.report import attained_level
from oska.conformance.runner import SuiteReport, run_directories
from oska.connectors.base import ConnectorError
from oska.connectors.registry import read_bindings
from oska.contracts.platform import (
    DomainSpec,
    HostIdentity,
    PackManifest,
    PackRef,
    PlatformManifest,
)
from oska.index.base import LearnedPhrases
from oska.index.discovery import Discovery
from oska.index.keyword import KeywordIndex
from oska.kit.scaffold import DEFAULT_HOST
from oska.platform.assembly import domain_guardrails
from oska.platform.errors import PackError, PlatformError
from oska.platform.packs import Pack, check_provides, check_requires, load_pack
from oska.platform.runtime import Platform

__all__ = ["SHELL_KIND", "PackReport", "check_pack", "one_pack_domain"]

SHELL_KIND = "shell"


class PackReport(BaseModel):
    """What checking one pack showed."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    path: Path
    manifest: PackManifest | None = None
    claimed_level: int = 0
    attained_level: int = 0
    errors: list[str] = Field(default_factory=list)
    validation_issues: list[ValidationIssue] = Field(default_factory=list)
    evaluation: CatalogEvaluation | None = None
    scenarios: SuiteReport | None = None
    ok: bool = False


def check_pack(
    path: Path | str, *, settings: Settings | None = None, allow_shell: bool = False
) -> PackReport:
    """Validate a pack, run its evaluation sets, and run its scenarios as a platform serves it.

    Each is run against the pack's own layout in isolation: its `skills/` as
    the only catalog root, its `configs/connectors.json`, its `scenarios/`,
    through the assembly of a domain serving this one pack. With a `shell`
    binding the scenarios are not run unless `allow_shell` is given.
    """
    root = Path(path)
    try:
        pack = load_pack(root)
    except PackError as exc:
        return PackReport(path=root, errors=[str(exc)])

    pack_settings = _settings_for(pack, settings)
    domain = one_pack_domain(pack)
    registry = SkillRegistry([pack.skills])
    errors: list[str] = []
    try:
        check_provides(pack, registry)
    except PackError as exc:
        errors.append(str(exc))
    try:
        check_requires(pack)
    except PackError as exc:
        errors.append(str(exc))
    guardrail_ids = _guardrail_ids(pack, domain, pack_settings, errors)
    skills = registry.skills()
    validation_issues = list(registry.parse_errors())
    validation_issues.extend(
        issue
        for skill in skills
        for issue in validate(
            skill, skills, schema_dir=pack_settings.schemas_dir, guardrail_ids=guardrail_ids
        )
    )
    refused = _refused_bindings(pack, allow_shell)
    errors.extend(refused)

    blocking = [issue for issue in validation_issues if issue.severity == "error"]
    evaluation: CatalogEvaluation | None = None
    scenarios: SuiteReport | None = None
    reached = 0
    if not blocking:
        evaluation, scenarios, run_errors = _run_checks(
            pack, domain, pack_settings, registry, run_scenarios=not refused
        )
        errors.extend(error for error in run_errors if error not in errors)
        if scenarios is not None:
            reached = attained_level(scenarios)

    ok = (
        not errors
        and not blocking
        and (evaluation is None or evaluation.ok)
        and (scenarios is None or scenarios.passed)
        and reached >= pack.manifest.level
    )
    return PackReport(
        path=pack.root,
        manifest=pack.manifest,
        claimed_level=pack.manifest.level,
        attained_level=reached,
        errors=errors,
        validation_issues=validation_issues,
        evaluation=evaluation,
        scenarios=scenarios,
        ok=ok,
    )


def one_pack_domain(pack: Pack) -> DomainSpec:
    """The domain a platform serving only this pack would declare.

    It answers as the host the pack declares, or as the kit's default host
    when it declares none, and grants its bindings nothing from the environment.
    """
    return DomainSpec(
        name=pack.name,
        packs=[pack.name],
        host=pack.manifest.host or HostIdentity(name=DEFAULT_HOST),
    )


def _one_pack_platform(pack: Pack, domain: DomainSpec) -> PlatformManifest:
    return PlatformManifest(
        name=pack.name,
        packs={pack.name: PackRef(path=pack.root)},
        domains=[domain],
        default_domain=domain.name,
    )


def _guardrail_ids(
    pack: Pack, domain: DomainSpec, settings: Settings, errors: list[str]
) -> frozenset[str]:
    """What the guardrail catalog of the pack's domain defines: the platform's and the pack's."""
    platform_catalog = guardrail_catalog_path(
        load_runtime_config(settings=settings).guardrails, settings
    )
    try:
        return frozenset(domain_guardrails(domain, [pack], platform_catalog).ids())
    except PlatformError as exc:
        errors.append(str(exc))
        return declared_guardrail_ids(settings)


def _refused_bindings(pack: Pack, allow_shell: bool) -> list[str]:
    """A refusal naming every `shell` binding of the pack, unless shell is allowed."""
    if allow_shell:
        return []
    try:
        integrations = read_bindings(pack.connectors).get("integrations") or {}
    except (OSError, ValueError, ConnectorError) as exc:
        return [f"pack {pack.name}: {pack.connectors} cannot be read: {exc}"]
    shell = sorted(
        integration_id
        for integration_id, binding in dict(integrations).items()
        if isinstance(binding, dict) and binding.get("type") == SHELL_KIND
    )
    if not shell:
        return []
    return [
        f"pack {pack.name}: {', '.join(shell)} is bound to a shell command, which a check "
        "would run on this machine; the scenarios were not run (pass --allow-shell to run "
        "them for a pack you trust)"
    ]


def _settings_for(pack: Pack, settings: Settings | None) -> Settings:
    base = settings or load_settings()
    return base.model_copy(
        update={
            "skills_roots": [pack.skills],
            "connectors_config": pack.connectors,
            "scenarios_dir": pack.scenarios,
        }
    )


def _run_checks(
    pack: Pack,
    domain: DomainSpec,
    settings: Settings,
    registry: SkillRegistry,
    *,
    run_scenarios: bool,
) -> tuple[CatalogEvaluation | None, SuiteReport | None, list[str]]:
    errors: list[str] = []
    evaluation: CatalogEvaluation | None = None
    scenarios: SuiteReport | None = None
    with tempfile.TemporaryDirectory(prefix="oska-pack-check-") as workspace:
        state_settings = settings.model_copy(update={"state_dir": Path(workspace)})
        try:
            evaluation = evaluate_catalog(registry, _discovery(state_settings, registry))
        except EvaluationSetError as exc:
            errors.append(str(exc))
        if run_scenarios and pack.scenarios.is_dir():
            try:
                platform = Platform(
                    _one_pack_platform(pack, domain), state_settings, root=Path(workspace)
                )
            except PlatformError as exc:
                errors.append(str(exc))
                return evaluation, scenarios, errors
            try:
                scenarios = SuiteReport.of(
                    run_directories(
                        [pack.scenarios], platform.settings_for(), platform.builder()
                    )
                )
            except (CatalogError, PlatformError) as exc:
                errors.append(f"the catalog does not hold up: {exc}")
            except (OSError, ValueError) as exc:
                errors.append(f"the scenarios could not be read: {exc}")
            finally:
                platform.close()
    return evaluation, scenarios, errors


def _discovery(settings: Settings, registry: SkillRegistry) -> Discovery:
    """The ranking `oska evaluate catalog` runs with no model provider configured."""
    runtime = load_runtime_config(settings=settings)
    learned = LearnedPhrases(state_path(Path(settings.state_dir), runtime.improvement.learned_root))
    return Discovery(registry, KeywordIndex(), runtime.discovery, runtime.enrichment, learned)
