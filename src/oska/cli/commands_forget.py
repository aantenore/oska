"""Deletion on request, and the window, from a terminal.

Two commands, both destructive and neither reversible. `forget session` answers
a person who asked that the record of their conversation stop existing;
`forget expired` applies the configured window to every conversation past it.
Both remove every store that names the session together: its receipts, its
audit entries, its stored state, the judgments recorded against its turns, the
presentation preferences learned from them, where tracing is on the spans that
name it, and the proposals drafted from it with the change requests composed
from those. A change request that was applied is part of the catalog: its
package is removed and the record of the decision kept, and the proposal it
came from is redacted rather than deleted. A conversation kept in eight places
is only gone when it is gone from all eight.

With `--platform` and no `--domain` or `--tenant`, both commands reach every
(tenant, domain) the platform serves, each opened with its own settings and its
own storage namespace, exactly as the runtime opens them. A request to be
forgotten is about a person, not about one domain: a session id is not known to
belong to a single (tenant, domain) in advance, so every pair is asked and the
counts are summed. `--domain` or `--tenant` narrows the pairs reached the same
way they narrow every other command.

Neither is a turn and neither runs inside the serving process. A window is an
operator's job on an operator's schedule, and a request to be forgotten is a
decision somebody takes and can say they took.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

import typer
from pydantic import Field

from oska.audit.trace import JsonlTraceSink
from oska.cli.options import (
    DomainOption,
    JsonOption,
    PlatformOption,
    SkillsOption,
    TenantOption,
    fail,
    platform_for,
    settings_for,
)
from oska.config import RuntimeConfig, Settings, load_runtime_config, state_path
from oska.governance.retention import RetentionReport, expire, forget
from oska.improvement.feedback import FeedbackStore, feedback_root
from oska.improvement.preferences import PreferenceStore, preferences_root
from oska.learning.changes import ChangeRequestStore
from oska.learning.forgetting import ChangeRequestRecords, ProposalRecords
from oska.learning.proposals import ProposalStore
from oska.platform import Platform, PlatformError
from oska.services import PROPOSALS_DIRNAME
from oska.storage import DEFAULT_NAMESPACE, open_ledger, open_sessions, storage_namespace

__all__ = ["app"]

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Remove the record of a conversation, on request or once it is past the window.",
)

DaysOption = Annotated[
    int | None,
    typer.Option("--days", help="Window in days. Defaults to the configured retention."),
]


@app.command("session")
def forget_session(
    session_id: str,
    as_json: JsonOption = False,
    skills: SkillsOption = None,
    platform: PlatformOption = None,
    domain: DomainOption = None,
    tenant: TenantOption = None,
) -> None:
    """Remove everything recorded for one session, whatever the window says."""
    if platform is None:
        settings = settings_for(skills)
        report = forget(session_id, **_stores(load_runtime_config(settings=settings), settings))
        _print(report, as_json)
        return
    held, reached = _platform_reach(platform, domain, tenant, skills)
    per_pair = {pair: forget(session_id, **_stores_for(held, pair)) for pair in reached}
    _print_reach(per_pair, "forget", as_json)


@app.command("expired")
def forget_expired(
    days: DaysOption = None,
    as_json: JsonOption = False,
    skills: SkillsOption = None,
    platform: PlatformOption = None,
    domain: DomainOption = None,
    tenant: TenantOption = None,
) -> None:
    """Remove everything recorded for sessions past the window."""
    if platform is None:
        settings = settings_for(skills)
        runtime = load_runtime_config(settings=settings)
        window = runtime.retention.days if days is None else days
        report = expire(window, **_stores(runtime, settings))
        _print(report, as_json)
        if window <= 0 and not as_json:
            typer.echo("the configured window keeps everything, so nothing was removed")
        return
    held, reached = _platform_reach(platform, domain, tenant, skills)
    # Retention is not a tenant-safe key (see oska.platform.tenants), so the
    # window is the platform's own and the same for every pair reached.
    window = held.runtime.retention.days if days is None else days
    per_pair = {pair: expire(window, **_stores_for(held, pair)) for pair in reached}
    _print_reach(per_pair, "expire", as_json)
    if window <= 0 and not as_json:
        typer.echo("the configured window keeps everything, so nothing was removed")


class PlatformRetentionReport(RetentionReport):
    """A removal's counts, summed over every (tenant, domain) it reached."""

    reached: list[str] = Field(default_factory=list)
    """The `tenant/domain` pairs that held something, sorted."""


def _print(report: RetentionReport, as_json: bool) -> None:
    """Say what went, as JSON or as one line per store."""
    if as_json:
        typer.echo(report.model_dump_json(indent=2))
        return
    for session_id in report.sessions:
        typer.echo(session_id)
    typer.echo(
        f"{len(report.sessions)} session(s): {report.receipts} receipt(s), "
        f"{report.events} audit event(s), {report.session_states} stored state(s), "
        f"{report.feedback} judgment(s), {report.preferences} preference(s), "
        f"{report.trace_spans} trace span(s), {report.proposals} proposal(s), "
        f"{report.change_requests} change request(s)"
    )


def _print_reach(
    per_pair: dict[tuple[str, str], RetentionReport],
    operation: Literal["forget", "expire"],
    as_json: bool,
) -> None:
    """Say what went across every (tenant, domain) reached, and which ones held anything."""
    combined = _combine(per_pair, operation)
    if as_json:
        typer.echo(combined.model_dump_json(indent=2))
        return
    _print(combined, as_json=False)
    if combined.reached:
        typer.echo(f"held something in: {', '.join(combined.reached)}")
    else:
        typer.echo("held nothing in any (tenant, domain) this platform serves")


def _combine(
    per_pair: dict[tuple[str, str], RetentionReport], operation: Literal["forget", "expire"]
) -> PlatformRetentionReport:
    sessions: list[str] = []
    seen: set[str] = set()
    for report in per_pair.values():
        for session_id in report.sessions:
            if session_id not in seen:
                seen.add(session_id)
                sessions.append(session_id)
    reports = list(per_pair.values())
    return PlatformRetentionReport(
        operation=operation,
        sessions=sessions,
        receipts=sum(report.receipts for report in reports),
        events=sum(report.events for report in reports),
        session_states=sum(report.session_states for report in reports),
        feedback=sum(report.feedback for report in reports),
        preferences=sum(report.preferences for report in reports),
        trace_spans=sum(report.trace_spans for report in reports),
        proposals=sum(report.proposals for report in reports),
        change_requests=sum(report.change_requests for report in reports),
        reached=sorted(
            f"{tenant}/{domain}"
            for (tenant, domain), report in per_pair.items()
            if not report.empty
        ),
    )


def _platform_reach(
    platform: Path, domain: str | None, tenant: str | None, skills: list[Path] | None
) -> tuple[Platform, list[tuple[str, str]]]:
    """The platform and the (tenant, domain) pairs a command should reach."""
    if skills:
        fail("--platform names the catalog; drop --skills")
    held = platform_for(platform)
    try:
        reached = held.pairs(domain, tenant)
    except PlatformError as exc:
        fail(str(exc))
    return held, reached


def _stores(
    runtime: RuntimeConfig, settings: Settings, *, namespace: str = DEFAULT_NAMESPACE
) -> dict[str, object]:
    """Every store retention reaches, opened the way the runtime opens them."""
    proposals = ProposalStore(Path(settings.state_dir) / PROPOSALS_DIRNAME)
    changes = ChangeRequestStore(state_path(settings.state_dir, runtime.changes.root))
    return {
        "ledger": open_ledger(runtime, settings, namespace=namespace),
        "audit_path": state_path(settings.state_dir, runtime.audit.path),
        "sessions": open_sessions(runtime, settings, namespace=namespace),
        "feedback": FeedbackStore(feedback_root(settings.state_dir)),
        "preferences": PreferenceStore(preferences_root(settings.state_dir)),
        "traces": JsonlTraceSink(state_path(settings.state_dir, runtime.tracing.path)),
        "proposals": ProposalRecords(proposals, changes),
        "change_requests": ChangeRequestRecords(proposals, changes),
    }


def _stores_for(held: Platform, pair: tuple[str, str]) -> dict[str, object]:
    """The stores of one (tenant, domain) of a platform, opened under its own namespace."""
    tenant, domain = pair
    return _stores(
        held.runtime_for(tenant),
        held.settings_for(domain, tenant),
        namespace=storage_namespace(tenant, domain),
    )
