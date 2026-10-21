"""The states a proposal may move through, and the one function that moves it.

`draft` is the only state a proposal reaches on its own: it is what
`draft_from_session` produces. Every later state, `approved` and `published`
included, is reached only through `transition`, which refuses an edge that is
not in `ALLOWED`, refuses an empty actor and records who asked. No other
function in this package writes a state, and publication additionally re-runs
the quality checks and refuses on any issue.

Where publication lands has moved. It used to write the new version into a
catalog root, which made the process that answers people the process that
rewrote the catalog it answered from. It now composes a change request: the
same package, written under the change request root for a person to read, with
the request id recorded in the transition note. `transition` takes no catalog
root and cannot be given one. A caller that wants the package installed calls
`apply` on the change request store with the root it names, which is reachable from a
terminal and from nowhere in the engine, the HTTP application or a job.

Publication is also where the derived routing examples of the new procedure are
written, when a provider is given and enrichment is enabled. They are written
beside the package inside the change request, so they travel with it and land
in the catalog when a person applies it. They are written through the change
request store, so a package kept in a database carries them as a directory
does. That work follows the publication and never gates it: a provider that
fails, or a file that cannot be written, is recorded in the transition note and
leaves the composed package exactly as it is.

Once composed, the request is handed to a publisher. The default is the change
request root itself; a `GitPublisher` also writes the package into a new branch
of a repository for a person to review. That step, too, follows the
publication: a branch that cannot be made is said in the note, the request
stays open under the change request root, and `oska change publish` can try
again. Routing it in one language is what publication promised; routing it in
several is what enrichment adds.

A move is written only while the stored proposal is still in the state it was
decided from: the store checks and writes in one step. Two reviewers, or two
replicas, moving the same proposal at once both pass the check against what
they read, and the second write is refused with `LifecycleError`. Publication
writes its move before it composes anything, so only the move that won
composes a change request; if composing then fails, the proposal is put back
the way it was and can be published again.

Every transition records where its actor came from: an identity the channel
authenticated, the operator at a terminal, or a name the call merely stated.
A stated actor is recorded as given and marked unverified on the proposal's
history and on the change request, and a git publisher will not author a
commit as one unless the operator allows it explicitly.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from oska.catalog.enrichment import PhraseProvider, enrich, render_examples
from oska.catalog.loader import parse_skill_text
from oska.catalog.registry import Catalog
from oska.config import EnrichmentConfig
from oska.contracts.proposal import (
    VERIFIED_SOURCES,
    ActorSource,
    LifecycleError,
    ProposalState,
    ProposalTransition,
    SkillProposal,
)
from oska.contracts.skill import Skill
from oska.contracts.stores import ChangeRequestRecordStore, ProposalRecordStore
from oska.learning import quality
from oska.learning.changes import (
    ChangeRequest,
    ChangeRequestError,
    new_request,
)
from oska.learning.proposals import render_skill, split_skill
from oska.learning.publishers import ChangePublisher, DirectoryPublisher

__all__ = ["ALLOWED", "SKILL_FILENAME", "LifecycleError", "next_version", "transition"]

ALLOWED: dict[ProposalState, frozenset[ProposalState]] = {
    ProposalState.draft: frozenset({ProposalState.submitted, ProposalState.rejected}),
    ProposalState.submitted: frozenset({ProposalState.approved, ProposalState.rejected}),
    ProposalState.approved: frozenset({ProposalState.published, ProposalState.rejected}),
}

SKILL_FILENAME = "skill.md"


def transition(
    store: ProposalRecordStore,
    proposal_id: str,
    to: ProposalState | str,
    by: str,
    note: str = "",
    *,
    registry: Catalog,
    changes: ChangeRequestRecordStore,
    schema_dir: Path | None = None,
    provider: PhraseProvider | None = None,
    enrichment: EnrichmentConfig | None = None,
    languages: Sequence[str] = (),
    publisher: ChangePublisher | None = None,
    actor_source: ActorSource | str = ActorSource.stated,
) -> SkillProposal:
    """Move one proposal to another state and record who asked for it.

    `actor_source` says where `by` came from, and it defaults to `stated`: a
    caller that established the actor says so, and one that did not is never
    recorded as having done so.

    Reaching `published` composes a change request rather than writing into a
    catalog: there is no catalog root to pass here, and the effect of the last
    step is a package in the change request store that a person reads and applies.
    `publisher` then puts it in front of that person; the change request root
    is the default, and a git publisher adds a branch.

    The move is written only while the stored proposal is still in the state
    it was read in; a move another decision overtook raises `LifecycleError`.
    """
    actor = by.strip()
    if not actor:
        raise LifecycleError("a transition records the actor that asked for it")
    source = ActorSource(actor_source)
    target = ProposalState(to)
    proposal = store.get(proposal_id)
    prior = proposal.state
    if target not in ALLOWED.get(prior, frozenset()):
        raise LifecycleError(f"{prior.value} does not lead to {target.value}")
    if target is not ProposalState.published:
        _record(proposal, target, actor, note, source)
        store.transition(proposal, prior)
        return proposal
    proposal.quality = quality.check(
        proposal.skill_markdown,
        registry,
        policy=proposal.policy,
        schema_dir=schema_dir,
    )
    if not quality.passed(proposal.quality):
        store.transition(proposal, prior)
        listed = "; ".join(proposal.quality.issues)
        raise LifecycleError(f"the proposal does not pass its checks: {listed}")
    before = proposal.model_copy(deep=True)
    markdown, request = _prepare(proposal, registry, changes, actor, source)
    proposal.skill_markdown = markdown
    note = _and(note, f"change request {request.request_id} at {changes.location}")
    _record(proposal, target, actor, note, source)
    # The claim: of two publications decided at once, only the one written first goes on.
    store.transition(proposal, prior)
    try:
        composed = _compose(request, markdown, proposal, changes)
    except BaseException:
        store.transition(before, target)
        raise
    note = _published(note, request, changes, publisher or DirectoryPublisher())
    note = _with_enrichment(note, composed, request, changes, provider, enrichment, languages)
    if note != proposal.history[-1].note:
        proposal.history[-1].note = note
        store.transition(proposal, target)
    return proposal


def _record(
    proposal: SkillProposal, target: ProposalState, actor: str, note: str, source: ActorSource
) -> None:
    """Move the proposal in memory and append who moved it; the store writes it after."""
    proposal.state = target
    proposal.history.append(
        ProposalTransition(
            state=target,
            at=datetime.now(UTC),
            by=actor,
            note=note,
            actor_source=source,
            actor_verified=source in VERIFIED_SOURCES,
        )
    )


def next_version(registry: Catalog, skill_id: str) -> int:
    """The version a new copy of this skill takes: one past the highest on disk."""
    versions = [skill.version for skill in registry.skills() if skill.id == skill_id]
    return max(versions) + 1 if versions else 1


def _with_enrichment(
    note: str,
    skill: Skill,
    request: ChangeRequest,
    changes: ChangeRequestRecordStore,
    provider: PhraseProvider | None,
    config: EnrichmentConfig | None,
    languages: Sequence[str],
) -> str:
    """Write the derived routing examples of a freshly composed procedure.

    The files land beside the package inside the change request, so a person
    who applies the request gets them and a person who rejects it gets nothing.
    Whatever happens here is said in the note and nowhere else: the package
    already stands, and nothing about it is undone by a paraphrase that could
    not be produced.
    """
    if provider is None or config is None or not config.enabled:
        return note
    try:
        examples = enrich(skill, provider, config, languages)
        if not examples:
            return _and(note, "no routing examples were written: the provider returned none")
        changes.add_file(request.request_id, config.filename, render_examples(skill, examples))
    except Exception as exc:  # enrichment never blocks a publication
        return _and(note, f"the routing examples were not written: {type(exc).__name__}: {exc}")
    return _and(note, f"routing examples written for {', '.join(sorted(examples))}")


def _published(
    note: str,
    request: ChangeRequest,
    changes: ChangeRequestRecordStore,
    publisher: ChangePublisher,
) -> str:
    """Hand the composed request to its publisher, and say in the note what came of it."""
    try:
        published = publisher.publish(request, changes)
    except ChangeRequestError as exc:
        return _and(note, f"the {publisher.name} publisher did not publish it: {exc}")
    if published.branch:
        return _and(note, f"branch {published.branch} at {published.commit}")
    return note


def _and(note: str, addition: str) -> str:
    """The note the actor wrote, followed by what publication then reported."""
    return f"{note}; {addition}" if note.strip() else addition


def _prepare(
    proposal: SkillProposal,
    registry: Catalog,
    changes: ChangeRequestRecordStore,
    actor: str,
    source: ActorSource,
) -> tuple[str, ChangeRequest]:
    """The package text a person would install, and the request that will carry it.

    Nothing is written here. The version is still read from the catalog the
    runtime serves, so a request says which version it would become; whether
    that version is still free is settled again by `apply`, which refuses to
    overwrite one that is there.
    """
    frontmatter, body = split_skill(proposal.skill_markdown)
    skill = parse_skill_text(proposal.skill_markdown, Path(changes.location) / SKILL_FILENAME)
    version = next_version(registry, skill.id)
    frontmatter["version"] = version
    frontmatter["status"] = "published"
    markdown = render_skill(frontmatter, body)
    request = new_request(
        proposal.proposal_id, skill.id, version, actor, proposal.quality, actor_source=source
    )
    return markdown, request


def _compose(
    request: ChangeRequest,
    markdown: str,
    proposal: SkillProposal,
    changes: ChangeRequestRecordStore,
) -> Skill:
    """Write the package under the change request root, and read it back as a skill."""
    where = changes.save(request, skill_markdown=markdown, policy=proposal.policy)
    path = Path(str(where)) / request.package / SKILL_FILENAME
    return parse_skill_text(markdown, path)
