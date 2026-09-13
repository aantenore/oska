"""The states a proposal may move through, and the one function that moves it.

`draft` is the only state a proposal reaches on its own: it is what
`draft_from_session` produces. Every later state, `approved` and `published`
included, is reached only through `transition`, which refuses an edge that is
not in `ALLOWED`, refuses an empty actor and records who asked. No other
function in this package writes a state, and publication additionally re-runs
the quality checks and refuses on any issue.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from opsdeck.catalog.loader import parse_skill_text
from opsdeck.catalog.registry import SkillRegistry
from opsdeck.contracts.proposal import ProposalState, ProposalTransition, SkillProposal
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
        proposal.skill_markdown = _publish(proposal, registry, Path(publish_root))
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


def _publish(proposal: SkillProposal, registry: SkillRegistry, publish_root: Path) -> str:
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
    (directory / SKILL_FILENAME).write_text(markdown, encoding="utf-8")
    return markdown
