"""Running turns from a terminal, and reading back what a session wrote.

One turn prints three things in the same order every time: the reply, then what
the session is waiting for, then one line per outcome. A write held at the gate
prints the exact payload that was frozen, so what is approved in a terminal is
the same text the ledger keys on.
"""

from __future__ import annotations

import json
import sys
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer

from opsdeck.cli.options import (
    JsonOption,
    SessionOption,
    SkillsOption,
    fail,
    services_for,
    settings_for,
)
from opsdeck.config import ProviderName, Settings, load_model_config
from opsdeck.contracts.common import Outcome, TurnResult
from opsdeck.contracts.receipt import WriteReceipt
from opsdeck.engine.engine import Engine
from opsdeck.planner.openai_compatible import OpenAICompatibleProvider
from opsdeck.planner.provider import ModelProvider, ModelProviderError
from opsdeck.planner.scripted import ScriptedModelProvider

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


def register(app: typer.Typer) -> None:
    """Add these commands to the application."""
    app.command()(run)
    app.command()(receipts)


def run(
    session: SessionOption = DEFAULT_SESSION,
    text: TextOption = None,
    provider: ProviderOption = None,
    script: ScriptOption = None,
    skills: SkillsOption = None,
) -> None:
    """Run one turn, or read turns from standard input until it ends."""
    settings = settings_for(skills)
    engine = services_for(settings, provider=_provider(settings, provider, script)).engine
    if text is not None:
        _turn(engine, session, text)
        return
    _repl(engine, session)


def receipts(
    session: SessionOption,
    as_json: JsonOption = False,
    skills: SkillsOption = None,
) -> None:
    """Print every write receipt of one session, oldest first."""
    held = services_for(settings_for(skills)).engine.receipts(session)
    if as_json:
        typer.echo(json.dumps([receipt.model_dump(mode="json") for receipt in held], indent=2))
        return
    for receipt in held:
        typer.echo(_receipt_line(receipt))


def _provider(
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


def _repl(engine: Engine, session: str) -> None:
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
        _turn(engine, session, text)


def _turn(engine: Engine, session: str, text: str) -> None:
    try:
        result = engine.turn(session, text)
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
