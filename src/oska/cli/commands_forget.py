"""Deletion on request, and the window, from a terminal.

Two commands, both destructive and neither reversible. `forget session` answers
a person who asked that the record of their conversation stop existing;
`forget expired` applies the configured window to every conversation past it.
Both remove every store that names the session together: its receipts, its
audit entries, its stored state, the judgments recorded against its turns, the
presentation preferences learned from them and, where tracing is on, the spans
that name it. A conversation kept in six files is only gone when it is gone
from all six.

Neither is a turn and neither runs inside the serving process. A window is an
operator's job on an operator's schedule, and a request to be forgotten is a
decision somebody takes and can say they took.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from oska.audit.trace import JsonlTraceSink
from oska.cli.options import JsonOption, SkillsOption, settings_for
from oska.config import Settings, load_runtime_config, state_path
from oska.engine.session_store import SessionStore
from oska.governance.ledger import WriteLedger
from oska.governance.retention import RetentionReport, expire, forget
from oska.improvement.feedback import FeedbackStore, feedback_root
from oska.improvement.preferences import PreferenceStore, preferences_root

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
) -> None:
    """Remove everything recorded for one session, whatever the window says."""
    settings = settings_for(skills)
    report = forget(
        session_id,
        ledger=_ledger(settings),
        audit_path=_audit_path(settings),
        sessions=_sessions(settings),
        feedback=FeedbackStore(feedback_root(settings.state_dir)),
        preferences=PreferenceStore(preferences_root(settings.state_dir)),
        traces=JsonlTraceSink(_trace_path(settings)),
    )
    _print(report, as_json)


@app.command("expired")
def forget_expired(
    days: DaysOption = None,
    as_json: JsonOption = False,
    skills: SkillsOption = None,
) -> None:
    """Remove everything recorded for sessions past the window."""
    settings = settings_for(skills)
    window = load_runtime_config(settings=settings).retention.days if days is None else days
    report = expire(
        window,
        ledger=_ledger(settings),
        audit_path=_audit_path(settings),
        sessions=_sessions(settings),
        feedback=FeedbackStore(feedback_root(settings.state_dir)),
        preferences=PreferenceStore(preferences_root(settings.state_dir)),
        traces=JsonlTraceSink(_trace_path(settings)),
    )
    _print(report, as_json)
    if window <= 0 and not as_json:
        typer.echo("the configured window keeps everything, so nothing was removed")


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
        f"{report.trace_spans} trace span(s)"
    )


def _ledger(settings: Settings) -> WriteLedger:
    runtime = load_runtime_config(settings=settings)
    return WriteLedger(
        state_path(settings.state_dir, runtime.ledger.path),
        busy_timeout=runtime.storage.busy_timeout_seconds,
    )


def _sessions(settings: Settings) -> SessionStore:
    runtime = load_runtime_config(settings=settings)
    return SessionStore(
        state_path(settings.state_dir, runtime.sessions.path),
        busy_timeout=runtime.storage.busy_timeout_seconds,
    )


def _trace_path(settings: Settings) -> Path:
    return state_path(settings.state_dir, load_runtime_config(settings=settings).tracing.path)


def _audit_path(settings: Settings) -> Path:
    return state_path(settings.state_dir, load_runtime_config(settings=settings).audit.path)
