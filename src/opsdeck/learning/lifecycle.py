"""The states a proposal may move through, and the one function that moves it.

`draft` is the only state a proposal reaches on its own: it is what
`draft_from_session` produces. Every later state, `approved` and `published`
included, is reached only through `transition`, which refuses an edge that is
not in `ALLOWED`, refuses an empty actor and records who asked. No other
function in this package writes a state, and publication additionally re-runs
the quality checks and refuses on any issue.

Publication is also where the derived routing examples of the new procedure are
written, when a provider is given and enrichment is enabled. That work follows
the publication and never gates it: a provider that fails, or a file that cannot
be written, is recorded in the transition note and leaves the published
procedure exactly as it is. Routing it in one language is what publication
promised; routing it in several is what enrichment adds.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from opsdeck.catalog.enrichment import PhraseProvider, enrich, write_examples
from opsdeck.catalog.loader import parse_skill_text
from opsdeck.catalog.registry import SkillRegistry
from opsdeck.config import EnrichmentConfig
from opsdeck.contracts.proposal import ProposalState, ProposalTransition, SkillProposal
from opsdeck.contracts.skill import Skill
from opsdeck.learning import quality
from opsdeck.learning.proposals import ProposalStore, render_skill, split_skill

ALLOWED: dict[ProposalState, frozenset[ProposalState]] = {
    ProposalState.draft: frozenset({ProposalState.submitted, ProposalState.rejected}),
    ProposalState.submitted: frozenset({ProposalState.approved, ProposalState.rejected}),
    ProposalState.approved: frozenset({ProposalState.published, ProposalState.rejected}),
}

SKILL_FILENAME = "skill.md"
POLICY_FILENAME = "policy.json"


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
    publish_root: Path,
    schema_dir: Path | None = None,
    provider: PhraseProvider | None = None,
    enrichment: EnrichmentConfig | None = None,
    languages: Sequence[str] = (),
) -> SkillProposal:
    """Move one proposal to another state and record who asked for it."""
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
        markdown, published = _publish(proposal, registry, Path(publish_root))
        proposal.skill_markdown = markdown
        note = _with_enrichment(note, published, provider, enrichment, languages)
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
    """Write the derived routing examples of a freshly published procedure.

    Whatever happens here is said in the note and nowhere else: the publication
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


def _publish(
    proposal: SkillProposal,
    registry: SkillRegistry,
    publish_root: Path,
) -> tuple[str, Skill]:
    frontmatter, body = split_skill(proposal.skill_markdown)
    skill = parse_skill_text(proposal.skill_markdown, publish_root / SKILL_FILENAME)
    version = next_version(registry, skill.id)
    frontmatter["version"] = version
    frontmatter["status"] = "published"
    markdown = render_skill(frontmatter, body)
    directory = publish_root / skill.type.value / skill.id.split(".", 1)[1] / f"v{version}"
    directory.mkdir(parents=True, exist_ok=True)
    if proposal.policy is not None:
        (directory / POLICY_FILENAME).write_text(
            json.dumps(proposal.policy, indent=2) + "\n", encoding="utf-8"
        )
    path = directory / SKILL_FILENAME
    path.write_text(markdown, encoding="utf-8")
    return markdown, parse_skill_text(markdown, path)
