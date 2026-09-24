"""Running turns from a terminal, and reading back what a session wrote.

One turn prints three things in the same order every time: the reply, then what
the session is waiting for, then one line per outcome. A write held at the gate
prints the exact payload that was frozen, so what is approved in a terminal is
the same text the ledger keys on.

With `--platform`, turns run in one domain of the platform, as one tenant, and
a session that already belongs to another domain is refused.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer

from oska.catalog.validator import CatalogError
from oska.cli.options import (
    DomainOption,
    JsonOption,
    PlatformOption,
    SessionOption,
    SkillsOption,
    TenantOption,
    fail,
    platform_for,
    services_for,
    settings_for,
)
from oska.config import ProviderName, Settings, load_model_config, load_settings
from oska.contracts.common import Outcome, TurnResult
from oska.contracts.receipt import WriteReceipt
from oska.engine.engine import Engine
from oska.planner.openai_compatible import OpenAICompatibleProvider
from oska.planner.provider import ModelProvider, ModelProviderError
from oska.planner.recording import RecordingProvider, ReplayProvider
from oska.planner.scripted import ScriptedModelProvider
from oska.platform import Platform, PlatformError

__all__ = ["receipts", "register", "run"]

DEFAULT_SESSION = "cli"
QUIT = "/quit"
PROMPT = "> "
_COMPACT = (",", ":")


class Provider(StrEnum):
    """Which cognition the run uses."""

    openai_compatible = "openai_compatible"
    scripted = "scripted"
    none = "none"


ProviderOption = Annotated[
    Provider | None,
    typer.Option("--provider", help="Which model provider to run with."),
]
ScriptOption = Annotated[
    Path | None,
    typer.Option("--script", help="Recorded answers for the scripted provider."),
]
TextOption = Annotated[
    str | None,
    typer.Option("--text", help="One turn. Without it the command reads standard input."),
]
RecordOption = Annotated[
    Path | None,
    typer.Option("--record", help="Write every model call to this cassette, appending to it."),
]
ReplayOption = Annotated[
    Path | None,
    typer.Option("--replay", help="Answer from this cassette instead of a provider."),
]


def register(app: typer.Typer) -> None:
    """Add these commands to the application."""
    app.command()(run)
    app.command()(receipts)


def run(
    session: SessionOption = DEFAULT_SESSION,
    text: TextOption = None,
    provider: ProviderOption = None,
    script: ScriptOption = None,
    record: RecordOption = None,
    replay: ReplayOption = None,
    skills: SkillsOption = None,
    platform: PlatformOption = None,
    domain: DomainOption = None,
    tenant: TenantOption = None,
) -> None:
    """Run one turn, or read turns from standard input until it ends."""
    if platform is None:
        settings = settings_for(skills, domain=domain, tenant=tenant)
        chosen = _provider(settings, provider, script, record, replay)
        turn: Turn = services_for(settings, provider=chosen).engine.turn
    else:
        if skills:
            fail("--platform names the catalog; drop --skills")
        chosen = _provider(load_settings(), provider, script, record, replay)
        turn = _platform_turn(platform, domain, tenant, chosen)
    if text is not None:
        _turn(turn, session, text)
        return
    _repl(turn, session)


def receipts(
    session: SessionOption,
    as_json: JsonOption = False,
    skills: SkillsOption = None,
    platform: PlatformOption = None,
    domain: DomainOption = None,
    tenant: TenantOption = None,
) -> None:
    """Print every write receipt of one session, oldest first."""
    if platform is None:
        engine = services_for(settings_for(skills, domain=domain, tenant=tenant)).engine
    else:
        engine = _platform_engine(platform, domain, tenant)
    held = engine.receipts(session)
    if as_json:
        typer.echo(json.dumps([receipt.model_dump(mode="json") for receipt in held], indent=2))
        return
    for receipt in held:
        typer.echo(_receipt_line(receipt))


Turn = Callable[[str, str], TurnResult]


def _platform_turn(
    platform: Path, domain: str | None, tenant: str | None, provider: ModelProvider | None
) -> Turn:
    """Turns of one domain of a platform, as one tenant, held to the session's domain."""
    held = platform_for(platform, provider=provider)
    _platform_engine(platform, domain, tenant, held=held)

    def turn(session: str, text: str) -> TurnResult:
        try:
            return held.turn(domain, tenant, session, text)
        except PlatformError as exc:
            fail(str(exc))

    return turn


def _platform_engine(
    platform: Path, domain: str | None, tenant: str | None, *, held: Platform | None = None
) -> Engine:
    """The engine of one (tenant, domain); one that cannot be built stops here."""
    built = held or platform_for(platform)
    try:
        return built.engine(domain, tenant)
    except PlatformError as exc:
        fail(str(exc))
    except CatalogError as exc:
        fail(f"the catalog does not hold up: {exc}")


def _provider(
    settings: Settings,
    chosen: Provider | None,
    script: Path | None,
    record: Path | None = None,
    replay: Path | None = None,
) -> ModelProvider | None:
    """The provider the run uses: replayed, recorded around, or as configured."""
    if record is not None and replay is not None:
        fail("--record writes a cassette and --replay reads one; use one of them")
    if replay is not None:
        try:
            return ReplayProvider(replay)
        except ModelProviderError as exc:
            fail(f"the cassette could not be read: {exc}")
    inner = _configured_provider(settings, chosen, script)
    if record is None:
        return inner
    if inner is None:
        fail("--record needs a provider to record; the provider in force is none")
    try:
        return RecordingProvider(inner, record)
    except (OSError, ModelProviderError) as exc:
        fail(f"the cassette could not be opened: {exc}")


def _configured_provider(
    settings: Settings, chosen: Provider | None, script: Path | None
) -> ModelProvider | None:
    """The provider the flags ask for, else the one the settings name, else none."""
    name: ProviderName = chosen.value if chosen is not None else (settings.provider or "none")
    if name == "none":
        return None
    if name == "scripted":
        if script is None:
            fail("the scripted provider needs --script pointing at a recorded script")
        try:
            return ScriptedModelProvider(script)
        except (OSError, ModelProviderError, json.JSONDecodeError) as exc:
            fail(f"the script could not be read: {exc}")
    try:
        return OpenAICompatibleProvider.from_config(load_model_config(settings=settings))
    except (OSError, ModelProviderError) as exc:
        fail(f"the model provider could not be built: {exc}")


def _repl(turn: Turn, session: str) -> None:
    """Read one request per line until the input ends or the reader asks to stop."""
    while True:
        typer.echo(PROMPT, nl=False, err=True)
        line = sys.stdin.readline()
        if not line:
            return
        text = line.strip()
        if not text:
            continue
        if text == QUIT:
            return
        _turn(turn, session, text)


def _turn(turn: Turn, session: str, text: str) -> None:
    try:
        result = turn(session, text)
    except ModelProviderError as exc:
        fail(f"the model provider failed: {exc}")
    _print(result)


def _print(result: TurnResult) -> None:
    typer.echo(result.reply)
    for line in _pending_lines(result):
        typer.echo(line)
    for outcome in result.outcomes:
        typer.echo(_outcome_line(outcome))


def _pending_lines(result: TurnResult) -> list[str]:
    pending = result.pending
    if pending is None:
        return []
    if pending.kind == "input":
        return [f"question: {pending.question or ''}"]
    approval = pending.approval
    if approval is None:
        return ["awaiting approval"]
    lines = [
        f"awaiting approval {approval.approval_id}",
        f"  {approval.integration_id} {approval.operation} {_compact(approval.exact_payload)}",
        f"  approvals {approval.approvals_given} of {approval.approvals_required}",
    ]
    return lines


def _outcome_line(outcome: Outcome) -> str:
    parts = [outcome.kind]
    if outcome.integration_id and outcome.operation:
        parts.append(f"{outcome.integration_id} {outcome.operation}")
    if outcome.receipt_id:
        parts.append(outcome.receipt_id)
    if outcome.reason:
        parts.append(outcome.reason)
    return "  ".join(parts)


def _receipt_line(receipt: WriteReceipt) -> str:
    return "  ".join(
        (
            receipt.receipt_id,
            receipt.integration_id,
            receipt.operation,
            _compact(receipt.payload),
            receipt.status,
            str(receipt.verification),
        )
    )


def _compact(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, separators=_COMPACT)
