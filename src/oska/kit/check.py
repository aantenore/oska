"""Checking one pack in isolation: validate it, evaluate it, run its scenarios.

`check_pack` is what `oska new pack` promises when it says the pack it writes
passes immediately, and what a pack author runs after every change. It answers
one question a person cannot answer by reading the files: does this pack, run
on its own with its own connectors, reach the conformance level it claims.

The pack is run the way a platform serves it: as the one pack of a domain,
assembled by `oska.platform`, so its guardrails, synonyms and host reach the
engine its scenarios run on, its skills are validated against the guardrail
catalog that domain would hold, its `phrases.json`, when it ships one, is
checked the way a platform checks it, and the level it reports is the level a
platform will measure. The bindings are held to what a platform holds them to:
a mock fixture is read only from inside the pack and nothing is read from the
environment. A check runs the pack's bindings, and a pack may come from anyone,
so a binding that runs a command on this machine, a `shell` binding or a tool
server started over stdio, is refused unless the caller allows it.

A tool server the check may reach is asked which tools it lists, and an
operation bound to a tool it does not list is an error. One that cannot be
reached is not checked, and the report says so in a warning: a check is not
the place to decide a server is wrong because it is down.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from oska.catalog.evaluation import CatalogEvaluation, EvaluationSetError, evaluate_catalog
from oska.catalog.registry import Catalog, SkillRegistry
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
from oska.connectors.mcp import McpConnector
from oska.connectors.registry import ConnectorRegistry, read_bindings
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
from oska.platform.assembly import domain_guardrails, domain_phrases
from oska.platform.errors import PackError, PlatformError
from oska.platform.packs import Pack, check_provides, check_requires, load_pack
from oska.platform.runtime import Platform

__all__ = ["MCP_KIND", "SHELL_KIND", "PackReport", "check_pack", "one_pack_domain"]

SHELL_KIND = "shell"
MCP_KIND = "mcp"
TOOLS_UNCHECKED = "tools_unchecked"


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
    through the assembly of a domain serving this one pack. With a binding that
    runs a command, `shell` or a tool server over stdio, the scenarios are not
    run unless `allow_shell` is given.
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
    try:
        domain_phrases(domain, [pack], load_runtime_config(settings=pack_settings), pack_settings)
    except PlatformError as exc:
        errors.append(str(exc))
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
    if not refused:
        tool_errors, notes = _tool_check(pack, domain)
        errors.extend(tool_errors)
        validation_issues.extend(notes)

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


def _runs_a_command(binding: object) -> bool:
    """Whether a binding starts a process on this machine when it is called."""
    if not isinstance(binding, dict):
        return False
    kind = binding.get("type")
    return kind == SHELL_KIND or (kind == MCP_KIND and binding.get("transport") == "stdio")


def _refused_bindings(pack: Pack, allow_shell: bool) -> list[str]:
    """A refusal naming every binding of the pack that runs a command, unless that is allowed."""
    if allow_shell:
        return []
    try:
        integrations = read_bindings(pack.connectors).get("integrations") or {}
    except (OSError, ValueError, ConnectorError) as exc:
        return [f"pack {pack.name}: {pack.connectors} cannot be read: {exc}"]
    shell = sorted(
        integration_id
        for integration_id, binding in dict(integrations).items()
        if _runs_a_command(binding)
    )
    if not shell:
        return []
    return [
        f"pack {pack.name}: {', '.join(shell)} is bound to a shell command or a tool server "
        "started over stdio, which a check would run on this machine; the scenarios were not "
        "run (pass --allow-shell to run them for a pack you trust)"
    ]


def _tool_check(pack: Pack, domain: DomainSpec) -> tuple[list[str], list[ValidationIssue]]:
    """Errors for operations bound to tools a server does not list, and notes for servers
    that could not be asked."""
    try:
        integrations = read_bindings(pack.connectors).get("integrations") or {}
    except (OSError, ValueError, ConnectorError):
        return [], []
    servers = {
        integration_id: binding
        for integration_id, binding in dict(integrations).items()
        if isinstance(binding, dict) and binding.get("type") == MCP_KIND
    }
    if not servers:
        return [], []
    try:
        registry = ConnectorRegistry.from_document(
            {"integrations": servers},
            pack.connectors.parent,
            readable=domain.environment.allows,
            confine_to=pack.root,
        )
    except ConnectorError as exc:
        return [f"pack {pack.name}: {exc}"], []
    errors: list[str] = []
    notes: list[ValidationIssue] = []
    try:
        for integration_id in registry.ids():
            connector = registry.get(integration_id)
            if not isinstance(connector, McpConnector):
                continue
            try:
                missing = connector.unlisted_tools()
            except ConnectorError as exc:
                notes.append(
                    ValidationIssue(
                        skill_id=integration_id,
                        code=TOOLS_UNCHECKED,
                        severity="warning",
                        path=pack.connectors,
                        message=f"the tool server could not be asked for its tools, so the "
                        f"tools its operations name were not checked: {exc}",
                    )
                )
                continue
            errors.extend(
                f"pack {pack.name}: {integration_id}: {operation} is bound to the tool "
                f"{tool}, which the server does not list"
                for operation, tool in missing
            )
    finally:
        registry.close()
    return errors, notes


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
    registry: Catalog,
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


def _discovery(settings: Settings, registry: Catalog) -> Discovery:
    """The ranking `oska evaluate catalog` runs with no model provider configured."""
    runtime = load_runtime_config(settings=settings)
    learned = LearnedPhrases(state_path(Path(settings.state_dir), runtime.improvement.learned_root))
    return Discovery(registry, KeywordIndex(), runtime.discovery, runtime.enrichment, learned)
