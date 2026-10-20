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
from urllib.parse import quote

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
from oska.config import (
    ProviderName,
    Settings,
    load_model_config,
    load_settings,
    state_path,
)
from oska.contracts.common import Outcome, TurnResult
from oska.contracts.receipt import WriteReceipt
from oska.engine.engine import Engine
from oska.planner.provider import ModelProvider, ModelProviderError
from oska.planner.providers import build_provider
from oska.planner.recording import RecordingProvider, ReplayProvider, cassette_digest
from oska.planner.scripted import ScriptedModelProvider, ScriptMismatchError
from oska.platform import Platform, PlatformError

__all__ = ["ReplayProgress", "receipts", "register", "run"]

DEFAULT_SESSION = "cli"
REPLAY_ROOT = Path("replay")
DIGEST_LENGTH = 16
QUIT = "/quit"
PROMPT = "> "
_COMPACT = (",", ":")


class Provider(StrEnum):
    """Which cognition the run uses."""

    openai_compatible = "openai_compatible"
    anthropic = "anthropic"
    profiles = "profiles"
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
ReplayFromStartOption = Annotated[
    bool,
    typer.Option(
        "--replay-from-start",
        help=(
            "With --replay and --session, ignore the position stored by earlier runs "
            "and start at the first recorded completion."
        ),
    ),
]
VolatileOption = Annotated[
    list[str] | None,
    typer.Option(
        "--volatile",
        help=(
            "Regex a recorded expectation is cut before the first match of; repeatable, "
            "replaces the default uuid patterns. Used with --record or --replay."
        ),
    ),
]


def register(app: typer.Typer) -> None:
    """Add these commands to the application."""
    app.command()(run)
    app.command()(receipts)


def run(
    ctx: typer.Context,
    session: SessionOption = DEFAULT_SESSION,
    text: TextOption = None,
    provider: ProviderOption = None,
    script: ScriptOption = None,
    record: RecordOption = None,
    replay: ReplayOption = None,
    replay_from_start: ReplayFromStartOption = False,
    volatile: VolatileOption = None,
    skills: SkillsOption = None,
    platform: PlatformOption = None,
    domain: DomainOption = None,
    tenant: TenantOption = None,
) -> None:
    """Run one turn, or read turns from standard input until it ends."""
    source = ctx.get_parameter_source("session")
    named = source is not None and source.name != "DEFAULT"
    keep = named and replay is not None and record is None
    progress: ReplayProgress | None = None
    if platform is None:
        settings = settings_for(skills, domain=domain, tenant=tenant)
        if keep and replay is not None:
            progress = ReplayProgress.open(settings, replay, session, replay_from_start)
        chosen = _provider(settings, provider, script, record, replay, volatile, progress)
        turn: Turn = services_for(settings, provider=chosen).engine.turn
    else:
        if skills:
            fail("--platform names the catalog; drop --skills")
        if keep and replay is not None:
            held = settings_for(platform=platform, domain=domain, tenant=tenant)
            progress = ReplayProgress.open(held, replay, session, replay_from_start)
        chosen = _provider(load_settings(), provider, script, record, replay, volatile, progress)
        turn = _platform_turn(platform, domain, tenant, chosen)
    try:
        if text is not None:
            _turn(turn, session, text)
            return
        _repl(turn, session)
    finally:
        if progress is not None and isinstance(chosen, ReplayProvider):
            progress.save(chosen.position)


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


class ReplayProgress:
    """Where a replayed session stands in its cassette, kept between processes.

    The position is a count of spent completions, stored under the state
    directory, in a file named by the session and by the hash of the cassette's
    content. A cassette that changed has another hash, so progress made against
    the old content is never applied to the new.
    """

    def __init__(self, path: Path, start: int) -> None:
        self.path = path
        self.start = start

    @classmethod
    def open(
        cls, settings: Settings, cassette: Path, session: str, from_start: bool
    ) -> ReplayProgress | None:
        """The progress of one session in one cassette; none if the cassette cannot be read.

        An unreadable cassette is left to the replay provider to report, so the
        message is the same with and without a stored position.
        """
        try:
            digest = cassette_digest(cassette)
        except ModelProviderError:
            return None
        root = state_path(settings.state_dir, REPLAY_ROOT) / digest[:DIGEST_LENGTH]
        path = root / f"{quote(session, safe='')}.json"
        return cls(path, 0 if from_start else _stored_position(path))

    def save(self, position: int) -> None:
        """Write the position reached."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({"position": position}) + "\n", encoding="utf-8")
        except OSError as exc:
            fail(f"the replay position could not be stored: {exc}")


def _stored_position(path: Path) -> int:
    """The position stored in `path`, or 0 when nothing was stored yet."""
    if not path.is_file():
        return 0
    try:
        position = json.loads(path.read_text(encoding="utf-8"))["position"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        fail(f"the replay position in {path} is unreadable ({exc}); use --replay-from-start")
    if not isinstance(position, int) or isinstance(position, bool) or position < 0:
        fail(f"the replay position in {path} is not a count; use --replay-from-start")
    return position


def _provider(
    settings: Settings,
    chosen: Provider | None,
    script: Path | None,
    record: Path | None = None,
    replay: Path | None = None,
    volatile: list[str] | None = None,
    progress: ReplayProgress | None = None,
) -> ModelProvider | None:
    """The provider the run uses: replayed, recorded around, or as configured."""
    if record is not None and replay is not None:
        fail("--record writes a cassette and --replay reads one; use one of them")
    if volatile is not None and record is None and replay is None:
        fail("--volatile only applies with --record or --replay")
    if replay is not None:
        try:
            return ReplayProvider(
                replay, volatile=volatile, start=progress.start if progress else 0
            )
        except ModelProviderError as exc:
            fail(f"the cassette could not be read: {exc}")
    inner = _configured_provider(settings, chosen, script)
    if record is None:
        return inner
    if inner is None:
        fail("--record needs a provider to record; the provider in force is none")
    try:
        return RecordingProvider(inner, record, volatile=volatile)
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
        model_config = load_model_config(settings=settings)
        return build_provider(model_config.model_copy(update={"provider": name}))
    except (OSError, ValueError, ModelProviderError) as exc:
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
    except ScriptMismatchError as exc:
        fail(f"the recorded answers no longer match: {exc}")
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
    parts: list[str] = [outcome.kind]
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
