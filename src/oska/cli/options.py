"""Options every command shares, and the way a command reports a failure.

`--skills` is read the same way everywhere: it replaces the catalog roots of
the settings, so a command that builds a registry and a command that builds a
whole runtime read the same catalog. `--connectors` does the same for the file
that binds the declared integrations, because a catalog and the bindings of its
operations travel together: a command pointed at one domain's skills has to be
pointed at that domain's connectors too. Expected failures leave one line on
standard error and a non-zero status; nothing prints a traceback.

`--platform` names a platform manifest instead (or `OSKA_PLATFORM` does, for
every command at once), `--domain` one of its domains and `--tenant` one of
that domain's tenants. The settings a command then reads are those of that
(tenant, domain): its packs' catalogs and bindings, and its own state
directory. Without `--platform` nothing changes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, NoReturn

import typer

from oska.catalog.registry import SkillRegistry
from oska.catalog.resolver import CapabilityResolver
from oska.catalog.validator import CatalogError
from oska.config import Settings, load_settings
from oska.engine.phrases import PhraseCatalogError
from oska.governance.guardrails import GuardrailError
from oska.planner.provider import ModelProvider
from oska.platform import Platform, PlatformError
from oska.platform.host import HostedResolver
from oska.services import Services, build_platform, build_services

__all__ = [
    "PLATFORM_ENV",
    "ConnectorsOption",
    "DomainOption",
    "JsonOption",
    "PlatformOption",
    "SchemasOption",
    "SessionOption",
    "SkillsOption",
    "TenantOption",
    "fail",
    "platform_for",
    "registry_for",
    "resolver_for",
    "services_for",
    "settings_for",
]

PLATFORM_ENV = "OSKA_PLATFORM"

SkillsOption = Annotated[
    list[Path] | None,
    typer.Option("--skills", help="Catalog root. Repeat for more than one."),
]
ConnectorsOption = Annotated[
    Path | None,
    typer.Option("--connectors", help="File binding the declared integrations to connectors."),
]
SchemasOption = Annotated[
    Path | None,
    typer.Option("--schemas", help="Directory holding the JSON schemas."),
]
SessionOption = Annotated[
    str,
    typer.Option("--session", help="Session the turns belong to."),
]
JsonOption = Annotated[
    bool,
    typer.Option("--json", help="Print the answer as JSON."),
]
PlatformOption = Annotated[
    Path | None,
    typer.Option(
        "--platform", envvar=PLATFORM_ENV, help="Platform manifest serving several domains."
    ),
]
DomainOption = Annotated[
    str | None,
    typer.Option("--domain", help="Domain of the platform. Defaults to its default domain."),
]
TenantOption = Annotated[
    str | None,
    typer.Option("--tenant", help="Tenant of the domain. Defaults to `default`."),
]


def settings_for(
    skills: list[Path] | None = None,
    connectors: Path | None = None,
    *,
    platform: Path | None = None,
    domain: str | None = None,
    tenant: str | None = None,
) -> Settings:
    """Settings from the environment, with the catalog and bindings the caller named.

    Given a platform, the settings of one of its (tenant, domain) pairs; a
    catalog or bindings named beside a platform would say two things at once.
    """
    if platform is not None:
        if skills or connectors is not None:
            fail("--platform names the catalog and the bindings; drop --skills and --connectors")
        held = platform_for(platform)
        try:
            return held.settings_for(domain, tenant)
        except PlatformError as exc:
            fail(str(exc))
    _refuse_scope_without_platform(domain, tenant)
    settings = load_settings()
    update: dict[str, object] = {}
    if skills:
        update["skills_roots"] = list(skills)
    if connectors is not None:
        update["connectors_config"] = Path(connectors)
    return settings.model_copy(update=update) if update else settings


def platform_for(path: Path, *, provider: ModelProvider | None = None) -> Platform:
    """The platform a manifest declares; one that does not hold up stops here."""
    try:
        return build_platform(path, load_settings(), provider=provider)
    except (PlatformError, GuardrailError, CatalogError, PhraseCatalogError) as exc:
        fail(f"the platform does not hold up: {exc}")


def resolver_for(
    skills: list[Path] | None = None,
    *,
    platform: Path | None = None,
    domain: str | None = None,
) -> CapabilityResolver:
    """The catalog wording a command prints: a domain's, named by its host, or the catalog's."""
    if platform is None:
        return CapabilityResolver(registry_for(settings_for(skills, domain=domain)))
    if skills:
        fail("--platform names the catalog; drop --skills")
    held = platform_for(platform)
    try:
        return HostedResolver(registry_for(held.settings_for(domain)), held.host(domain))
    except PlatformError as exc:
        fail(str(exc))


def registry_for(settings: Settings) -> SkillRegistry:
    """Registry over the catalog roots of these settings."""
    return SkillRegistry(settings.skills_roots)


def services_for(settings: Settings, *, provider: ModelProvider | None = None) -> Services:
    """The runtime these settings assemble; a catalog that does not hold up stops here."""
    try:
        return build_services(settings, provider=provider)
    except CatalogError as exc:
        fail(f"the catalog does not hold up: {exc}")
    except PhraseCatalogError as exc:
        fail(str(exc))


def fail(message: str) -> NoReturn:
    """Report an expected failure on one line and stop with a non-zero status."""
    typer.echo(message, err=True)
    raise typer.Exit(code=1)


def _refuse_scope_without_platform(domain: str | None, tenant: str | None) -> None:
    if domain is not None or tenant is not None:
        fail("--domain and --tenant name a part of a platform; name the platform with --platform")
