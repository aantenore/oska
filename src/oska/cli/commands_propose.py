"""The proposal lifecycle from a terminal: draft, list, show, move.

Nothing here decides a state. `transition` is the only way a proposal moves,
and it is called with the actor the caller named; a refused edge leaves the
message the lifecycle gave and a non-zero status, the same answer the HTTP
application returns as a conflict.

A move to `published` writes a change request, not a skill file. What that
request holds and where it can be installed is the business of the `change`
commands; these commands say which request was composed and stop there.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from oska.cli.options import SkillsOption, fail, services_for, settings_for
from oska.config import RuntimeConfig, load_runtime_config
from oska.contracts.proposal import SkillProposal
from oska.learning import LifecycleError, draft_from_session, store_for, transition
from oska.planner.provider import ModelProvider
from oska.services import Services

__all__ = ["propose"]

propose = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Draft a procedure from a session and move it through its lifecycle.",
)

ByOption = Annotated[str, typer.Option("--by", help="Who is asking for this.")]
StateOption = Annotated[
    str | None,
    typer.Option("--state", help="Show only proposals in this state."),
]
ToOption = Annotated[str, typer.Option("--to", help="State to move the proposal to.")]
NoteOption = Annotated[str, typer.Option("--note", help="Why, recorded with the transition.")]


@propose.command("from-session")
def from_session(
    session_id: str,
    by: ByOption = "",
    skills: SkillsOption = None,
) -> None:
    """Draft a candidate procedure from what one session did."""
    services = services_for(settings_for(skills))
    engine = services.engine
    planner = engine.planner
    proposal = draft_from_session(
        session_id,
        engine.state(session_id).history,
        engine.receipts(session_id),
        engine.registry,
        None if planner is None else planner.provider,
        schema_dir=services.settings.schemas_dir,
    )
    if by.strip():
        proposal.history[0].by = by.strip()
    services.proposals.save(proposal)
    typer.echo(f"{proposal.proposal_id}  {proposal.state.value}")
    for issue in proposal.quality.issues:
        typer.echo(f"  {issue}")


@propose.command("list")
def list_proposals(state: StateOption = None, skills: SkillsOption = None) -> None:
    """Print every proposal held, oldest file first."""
    services = _services(skills)
    try:
        held = services.proposals.list(state)
    except ValueError as exc:
        fail(str(exc))
    for proposal in held:
        typer.echo(_line(proposal))


@propose.command("show")
def show(proposal_id: str, skills: SkillsOption = None) -> None:
    """Print one proposal in full."""
    services = _services(skills)
    try:
        proposal = services.proposals.get(proposal_id)
    except KeyError as exc:
        fail(str(exc))
    typer.echo(json.dumps(proposal.model_dump(mode="json"), indent=2, ensure_ascii=False))


@propose.command("transition")
def move(
    proposal_id: str,
    to: ToOption,
    by: ByOption,
    note: NoteOption = "",
    skills: SkillsOption = None,
) -> None:
    """Move one proposal to another state, recording who asked."""
    services = _services(skills)
    changes = store_for(services.settings)
    try:
        proposal = transition(
            services.proposals,
            proposal_id,
            to,
            by,
            note,
            registry=services.engine.registry,
            changes=changes,
            schema_dir=services.settings.schemas_dir,
            provider=_provider_of(services),
            enrichment=_runtime_of(services).enrichment,
            languages=_runtime_of(services).discovery.languages,
        )
    except (KeyError, LifecycleError, ValueError) as exc:
        fail(str(exc))
    typer.echo(_line(proposal))
    for composed in changes.list():
        if composed.proposal_id == proposal.proposal_id:
            typer.echo(
                f"  change request {composed.request_id}  {composed.skill_id} "
                f"v{composed.version}  {composed.state.value}"
            )


def _services(skills: list[Path] | None) -> Services:
    return services_for(settings_for(skills))


def _line(proposal: SkillProposal) -> str:
    return "  ".join(
        (
            proposal.proposal_id,
            proposal.state.value,
            proposal.source_session_id,
            f"{len(proposal.quality.issues)} issue(s)",
        )
    )


def _runtime_of(services: Services) -> RuntimeConfig:
    """Runtime configuration behind an assembled service set."""
    return load_runtime_config(settings=services.settings)


def _provider_of(services: Services) -> ModelProvider | None:
    """The model provider in force, when one is configured."""
    planner = services.engine.planner
    return planner.provider if planner is not None else None
