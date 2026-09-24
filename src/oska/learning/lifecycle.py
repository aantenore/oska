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
`ChangeRequestStore.apply` with the root it names, which is reachable from a
terminal and from nowhere in the engine, the HTTP application or a job.

Publication is also where the derived routing examples of the new procedure are
written, when a provider is given and enrichment is enabled. They are written
beside the package inside the change request, so they travel with it and land
in the catalog when a person applies it. That work follows the publication and
never gates it: a provider that fails, or a file that cannot be written, is
recorded in the transition note and leaves the composed package exactly as it
is. Routing it in one language is what publication promised; routing it in
several is what enrichment adds.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from oska.catalog.enrichment import PhraseProvider, enrich, write_examples
from oska.catalog.loader import parse_skill_text
from oska.catalog.registry import SkillRegistry
from oska.config import EnrichmentConfig
from oska.contracts.proposal import ProposalState, ProposalTransition, SkillProposal
from oska.contracts.skill import Skill
from oska.learning import quality
from oska.learning.changes import ChangeRequest, ChangeRequestStore, new_request
from oska.learning.proposals import ProposalStore, render_skill, split_skill

ALLOWED: dict[ProposalState, frozenset[ProposalState]] = {
    ProposalState.draft: frozenset({ProposalState.submitted, ProposalState.rejected}),
    ProposalState.submitted: frozenset({ProposalState.approved, ProposalState.rejected}),
    ProposalState.approved: frozenset({ProposalState.published, ProposalState.rejected}),
}

SKILL_FILENAME = "skill.md"


class LifecycleError(RuntimeError):
    """A proposal cannot move the way it was asked to."""


def transition(
    store: ProposalStore,
    proposal_id: str,
    to: ProposalState | str,
    by: str,
    note: str = "",
    *,
    registry: SkillRegistry,
    changes: ChangeRequestStore,
    schema_dir: Path | None = None,
    provider: PhraseProvider | None = None,
    enrichment: EnrichmentConfig | None = None,
    languages: Sequence[str] = (),
) -> SkillProposal:
    """Move one proposal to another state and record who asked for it.

    Reaching `published` composes a change request rather than writing into a
    catalog: there is no catalog root to pass here, and the effect of the last
    step is a package under `changes.root` that a person reads and applies.
    """
    actor = by.strip()
    if not actor:
        raise LifecycleError("a transition records the actor that asked for it")
    target = ProposalState(to)
    proposal = store.get(proposal_id)
    if target not in ALLOWED.get(proposal.state, frozenset()):
        raise LifecycleError(f"{proposal.state.value} does not lead to {target.value}")
    if target is ProposalState.published:
        proposal.quality = quality.check(
            proposal.skill_markdown,
            registry,
            policy=proposal.policy,
            schema_dir=schema_dir,
        )
        if not quality.passed(proposal.quality):
            store.save(proposal)
            listed = "; ".join(proposal.quality.issues)
            raise LifecycleError(f"the proposal does not pass its checks: {listed}")
        markdown, composed, request = _compose(proposal, registry, changes, actor)
        proposal.skill_markdown = markdown
        note = _and(note, f"change request {request.request_id} at {changes.root}")
        note = _with_enrichment(note, composed, provider, enrichment, languages)
    proposal.state = target
    proposal.history.append(
        ProposalTransition(state=target, at=datetime.now(UTC), by=actor, note=note)
    )
    store.save(proposal)
    return proposal


def next_version(registry: SkillRegistry, skill_id: str) -> int:
    """The version a new copy of this skill takes: one past the highest on disk."""
    versions = [skill.version for skill in registry.skills() if skill.id == skill_id]
    return max(versions) + 1 if versions else 1


def _with_enrichment(
    note: str,
    skill: Skill,
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
        write_examples(skill, examples, config)
    except Exception as exc:  # enrichment never blocks a publication
        return _and(note, f"the routing examples were not written: {type(exc).__name__}: {exc}")
    return _and(note, f"routing examples written for {', '.join(sorted(examples))}")


def _and(note: str, addition: str) -> str:
    """The note the actor wrote, followed by what publication then reported."""
    return f"{note}; {addition}" if note.strip() else addition


def _compose(
    proposal: SkillProposal,
    registry: SkillRegistry,
    changes: ChangeRequestStore,
    actor: str,
) -> tuple[str, Skill, ChangeRequest]:
    """Write the package a person would install, under the change request root.

    The version is still read from the catalog the runtime serves, so a request
    says which version it would become; whether that version is still free is
    settled again by `apply`, which refuses to overwrite one that is there.
    """
    frontmatter, body = split_skill(proposal.skill_markdown)
    skill = parse_skill_text(proposal.skill_markdown, changes.root / SKILL_FILENAME)
    version = next_version(registry, skill.id)
    frontmatter["version"] = version
    frontmatter["status"] = "published"
    markdown = render_skill(frontmatter, body)
    request = new_request(
        proposal.proposal_id, skill.id, version, actor, proposal.quality
    )
    directory = changes.save(request, skill_markdown=markdown, policy=proposal.policy)
    path = directory / request.package / SKILL_FILENAME
    return markdown, parse_skill_text(markdown, path), request
