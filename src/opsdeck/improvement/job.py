"""The scheduled job: four steps, one report, and a ceiling it cannot lift.

The steps are in the order their evidence allows. Signals first, because
everything else reads them. Learned phrasings second, because they are the only
change the job makes and they are files it can delete again. Tuning and
evaluation third, over a labelled set a person wrote, because a number derived
from traffic the system routed itself would only confirm its own habits. It is
measured over the catalog as the runtime ranks it, phrases derived and phrases
just learned included, so the number says what routing does now and not what it
did before the job ran. Gaps
last, because a gap is what is left when the first three have explained
everything they can.

What the job may not do is the part worth reading. It drafts a proposal and
stops. It does not move one, it does not write a policy, and it does not touch a
connector binding. The lifecycle that would move a proposal is not imported
here.
"""

from __future__ import annotations

from pathlib import Path

from opsdeck.catalog.registry import SkillRegistry
from opsdeck.config import (
    RuntimeConfig,
    Settings,
    load_runtime_config,
    state_path,
)
from opsdeck.contracts.common import Message
from opsdeck.contracts.proposal import ProposalState, SkillProposal
from opsdeck.governance.ledger import WriteLedger
from opsdeck.improvement import learned as learned_phrases
from opsdeck.improvement.evaluation import RoutingCase, load_cases, run_evaluation
from opsdeck.improvement.gaps import Gap, find_gaps
from opsdeck.improvement.report import (
    DraftedProposal,
    ImprovementReport,
    read_report,
    write_report,
)
from opsdeck.improvement.signals import load_signals
from opsdeck.improvement.tuning import Tuning, rank_cases, tune, write_overrides
from opsdeck.index.base import LearnedPhrases, SkillIndex
from opsdeck.index.discovery import Discovery
from opsdeck.index.embedding import EmbeddingIndex
from opsdeck.index.keyword import KeywordIndex
from opsdeck.learning.proposals import ProposalStore, draft_from_session
from opsdeck.ontology.graph import OntologyGraph
from opsdeck.planner.provider import ModelProvider

__all__ = ["PROPOSALS_DIRNAME", "run_job"]

PROPOSALS_DIRNAME = "proposals"


def run_job(
    settings: Settings,
    *,
    provider: ModelProvider | None = None,
    apply_tuning: bool = False,
    cases_path: Path | None = None,
    redactions_path: Path | None = None,
    proposals: ProposalStore | None = None,
) -> ImprovementReport:
    """Run the four steps, write the report, and return it."""
    runtime = load_runtime_config(settings=settings)
    improvement = runtime.improvement
    state_dir = Path(settings.state_dir)
    registry = SkillRegistry(settings.skills_roots)
    ledger = WriteLedger(state_path(state_dir, runtime.ledger.path))

    signals = load_signals(state_path(state_dir, runtime.audit.path), ledger)
    kept = learned_phrases.learn(
        signals,
        registry,
        improvement,
        provider=provider,
        redactions_path=redactions_path,
    )
    learned_root = state_path(state_dir, improvement.learned_root)
    for skill_id, phrases in kept.items():
        learned_phrases.write_learned(learned_root, skill_id, phrases)

    discovery = Discovery(
        registry,
        _index(provider),
        runtime.discovery,
        runtime.enrichment,
        LearnedPhrases(learned_root),
    )
    cases = load_cases(cases_path)
    evaluation = run_evaluation(cases, discovery)
    tuning = _tune(cases, discovery, runtime, provider)
    overrides = write_overrides(state_dir, tuning) if apply_tuning and tuning else None

    graph = OntologyGraph.from_registry(registry)
    gaps = find_gaps(
        signals,
        graph,
        improvement,
        min_score=runtime.discovery.min_score,
        provider=provider,
    )
    store = proposals or ProposalStore(state_dir / PROPOSALS_DIRNAME)
    drafted = [_draft(gap, registry, store, settings, provider) for gap in gaps]

    report_path = state_path(state_dir, improvement.report_path)
    previous = read_report(report_path)
    report = ImprovementReport(
        signals=len(signals),
        grounded=sum(1 for signal in signals if signal.grounded),
        learned={skill_id: len(phrases) for skill_id, phrases in kept.items()},
        tuning=tuning,
        tuning_applied=overrides is not None,
        overrides_path=None if overrides is None else str(overrides),
        evaluation=evaluation,
        previous_precision=None if previous is None else previous.evaluation.precision,
        gaps=gaps,
        proposals=drafted,
    )
    write_report(report_path, report)
    return report


def _index(provider: ModelProvider | None) -> SkillIndex:
    """The same choice the runtime makes: embeddings when there is a provider."""
    return KeywordIndex() if provider is None else EmbeddingIndex(provider)


def _tune(
    cases: list[RoutingCase],
    discovery: Discovery,
    runtime: RuntimeConfig,
    provider: ModelProvider | None,
) -> Tuning | None:
    """Search the thresholds over the labelled set, or nothing when there is no set."""
    if not cases:
        return None
    ranked = rank_cases(
        cases,
        discovery.candidates(),
        _index(provider),
        runtime.discovery.limit,
        discovery.texts,
    )
    return tune(cases, ranked, runtime.discovery, runtime.improvement.bounds)


def _draft(
    gap: Gap,
    registry: SkillRegistry,
    store: ProposalStore,
    settings: Settings,
    provider: ModelProvider | None,
) -> DraftedProposal:
    """Write one candidate for one gap and leave it exactly where it is.

    The candidate carries no write, because nothing ran: the requests in a gap
    reached no procedure. It is a sentence for a reviewer, and the only state it
    is ever in here is the one `draft_from_session` gives it.
    """
    proposal = draft_from_session(
        _session_of(gap),
        [Message(role="user", content=phrase) for phrase in gap.sample_phrases],
        [],
        registry,
        provider,
        runbook_id_hint=_hint(gap),
        schema_dir=settings.schemas_dir,
    )
    _refuse_anything_but_a_draft(proposal)
    store.save(proposal)
    return DraftedProposal(
        proposal_id=proposal.proposal_id,
        skill_id=_skill_id_of(proposal),
        state=proposal.state.value,
        issues=len(proposal.quality.issues),
    )


def _refuse_anything_but_a_draft(proposal: SkillProposal) -> None:
    """The ceiling, checked at runtime and not only in a test."""
    if proposal.state is not ProposalState.draft:
        raise RuntimeError(
            "the improvement job produces candidates in one state only, "
            f"and this one is {proposal.state.value}"
        )


def _session_of(gap: Gap) -> str:
    """A gap has no session of its own; it is named after the pattern it is."""
    return f"gap:{_hint(gap)}"


def _hint(gap: Gap) -> str:
    """What to name the candidate: a declared operation, else the request itself."""
    if gap.suggested_operations:
        return gap.suggested_operations[0]
    return gap.sample_phrases[0] if gap.sample_phrases else "unmatched requests"


def _skill_id_of(proposal: SkillProposal) -> str:
    """The identifier the candidate declares, read back from its own frontmatter."""
    for line in proposal.skill_markdown.splitlines():
        if line.startswith("id:"):
            return line.split(":", 1)[1].strip()
    return ""
