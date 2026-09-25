"""Serving the HTTP application, and running a scenario directory against it.

The host and the port are configuration, not flags with defaults baked in: the
flags override what `runtime.json` says under `api`. `conform` runs the suite a
caller would run to decide whether this installation conforms, and leaves a
non-zero status when a scenario did not pass.

With `--platform`, `serve` answers in every domain of the platform and
`conform` runs each domain's scenarios with that domain's engine, reports the
level per domain, and fails when a domain falls short of the platform's target.

`serve` warns on stderr when it binds an address other people can reach while
the configuration does not require an identity: anyone who can reach the port
can then hold a conversation, and nothing but the network stands in the way.

`serve` reasons with the model provider `model.json` and `OSKA_PROVIDER`
describe, in both modes; with `none` it serves without one. A provider that
cannot be built stops the command with one line before anything listens.
"""

from __future__ import annotations

import ipaddress
import json
from pathlib import Path
from typing import Annotated

import typer

from oska.catalog.validator import CatalogError
from oska.cli.options import (
    ConnectorsOption,
    DomainOption,
    JsonOption,
    PlatformOption,
    SkillsOption,
    TenantOption,
    fail,
    platform_for,
    settings_for,
)
from oska.config import Settings, load_model_config, load_runtime_config, load_settings
from oska.conformance import render_json, render_text, run_suite
from oska.engine.phrases import PhraseCatalogError
from oska.planner.provider import ModelProvider, ModelProviderError
from oska.planner.providers import build_provider
from oska.platform import PlatformError
from oska.platform.conformance import (
    passed,
    render_platform_json,
    render_platform_text,
    run_platform,
)

__all__ = [
    "conform",
    "is_loopback",
    "open_port_warning",
    "register",
    "serve",
    "serving_provider",
]

HostOption = Annotated[
    str | None,
    typer.Option("--host", help="Address to listen on. Defaults to the configured one."),
]
PortOption = Annotated[
    int | None,
    typer.Option("--port", help="Port to listen on. Defaults to the configured one."),
]
ScenariosOption = Annotated[
    Path | None,
    typer.Option("--scenarios", help="Directory of scenario files."),
]


def register(app: typer.Typer) -> None:
    """Add these commands to the application."""
    app.command()(serve)
    app.command()(conform)


def serve(
    host: HostOption = None,
    port: PortOption = None,
    skills: SkillsOption = None,
    platform: PlatformOption = None,
) -> None:
    """Serve the HTTP application, over one catalog or every domain of a platform."""
    if platform is not None and skills:
        fail("--platform names the catalog; drop --skills")
    settings = settings_for(skills) if platform is None else load_settings()
    try:
        import uvicorn

        from oska.api.app import create_app
    except ImportError as exc:
        fail(f"serving needs the api extra: {exc}")
    runtime = load_runtime_config(settings=settings)
    configured = runtime.api
    bound_host = host or configured.host
    bound_port = port if port is not None else configured.port
    if not runtime.identity.required and not is_loopback(bound_host):
        typer.echo(open_port_warning(bound_host, bound_port), err=True)
    provider = serving_provider(settings)
    try:
        application = create_app(
            settings,
            platform=None if platform is None else platform_for(platform, provider=provider),
            provider=provider,
        )
    except CatalogError as exc:
        fail(f"the catalog does not hold up: {exc}")
    except PlatformError as exc:
        fail(f"the platform does not hold up: {exc}")
    except PhraseCatalogError as exc:
        fail(str(exc))
    uvicorn.run(application, host=bound_host, port=bound_port)


def serving_provider(settings: Settings) -> ModelProvider | None:
    """The model provider the configuration describes, or none; one that cannot be built stops."""
    try:
        return build_provider(load_model_config(settings=settings))
    except (OSError, ValueError, ModelProviderError) as exc:
        fail(f"the model provider could not be built: {exc}")


def is_loopback(host: str) -> bool:
    """Whether an address to listen on is reachable from this machine only."""
    if host.strip().lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def open_port_warning(host: str, port: int) -> str:
    """What `serve` says before it listens where others can reach it without an identity."""
    return (
        f"warning: listening on {host}:{port} with identity.required off. Anyone who can "
        "reach this port can hold a conversation and ask for writes. Set identity.mode "
        "to token in configs/runtime.json (identity.required then follows) and the key "
        "variable identity.key_env names, put an authenticating proxy in front and set "
        "identity.required to true, or listen on 127.0.0.1."
    )


def conform(
    scenarios: ScenariosOption = None,
    as_json: JsonOption = False,
    skills: SkillsOption = None,
    connectors: ConnectorsOption = None,
    platform: PlatformOption = None,
    domain: DomainOption = None,
    tenant: TenantOption = None,
) -> None:
    """Run a scenario directory and report the conformance level it reaches."""
    if platform is not None and scenarios is None and domain is None:
        _conform_platform(platform, tenant, as_json, skills, connectors)
        return
    settings = settings_for(skills, connectors, platform=platform, domain=domain, tenant=tenant)
    build = None
    if platform is not None:
        try:
            build = platform_for(platform).builder(domain, tenant)
        except PlatformError as exc:
            fail(str(exc))
    directory = Path(scenarios) if scenarios is not None else settings.scenarios_dir
    if not directory.is_dir():
        fail(f"no scenario directory at {directory}")
    try:
        report = run_suite(directory, settings, build=build)
    except CatalogError as exc:
        fail(f"the catalog does not hold up: {exc}")
    except (OSError, ValueError) as exc:
        fail(f"the scenarios could not be read: {exc}")
    if as_json:
        typer.echo(json.dumps(render_json(report), indent=2))
    else:
        typer.echo(render_text(report))
    if not report.passed:
        raise typer.Exit(code=1)


def _conform_platform(
    platform: Path,
    tenant: str | None,
    as_json: bool,
    skills: list[Path] | None,
    connectors: Path | None,
) -> None:
    """Every domain's scenarios, each with its own engine, and the level of each."""
    if skills or connectors is not None:
        fail("--platform names the catalog and the bindings; drop --skills and --connectors")
    held = platform_for(platform)
    try:
        reports = run_platform(held, tenant)
    except CatalogError as exc:
        fail(f"the catalog does not hold up: {exc}")
    except PlatformError as exc:
        fail(str(exc))
    except (OSError, ValueError) as exc:
        fail(f"the scenarios could not be read: {exc}")
    if as_json:
        typer.echo(json.dumps(render_platform_json(held, reports), indent=2))
    else:
        typer.echo(render_platform_text(held, reports))
    if not passed(held, reports):
        raise typer.Exit(code=1)
