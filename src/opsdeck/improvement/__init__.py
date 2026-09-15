"""The autonomy ladder: how far this system may improve itself without a person.

A system that learns from its own traffic has to say, once and in public, where
it stops. This package is that statement, written as code rather than as an
intention.

Level 0 — observe. Signals are read back from the audit trail and the write
ledger. Nothing is changed.

Level 1 — adjust what is cheap and reversible, and show every change. Learned
phrasings are written as separate files under the improvement root, one per
procedure, each revertible by deleting it. Threshold tuning is proposed as an
overrides file a caller may apply. Nothing authored is edited.

Level 2 — draft, and stop. A request that reached no procedure becomes a gap,
and a gap becomes a proposal in `draft` state with the entities and operations
the ontology already declares. A draft is a sentence addressed to a reviewer,
not a capability.

Level 3 — never without a person. Moving a proposal past `draft`, publishing
anything that can write, changing a policy, changing a connector binding: none
of it happens here, under any configuration, on any code path.

Hard invariant: no code path in this package reaches `submitted`, `approved` or
`published`; none writes a policy; none edits a connector configuration. The
lifecycle that moves a proposal is neither imported nor called from here, and a
test walks the syntax tree of every module in this package to keep it that way.
The boundary is the deliverable. The job is only what fits underneath it.
"""

from opsdeck.improvement.evaluation import (
    TIE,
    EvaluationReport,
    RoutingCase,
    load_cases,
    run_evaluation,
)
from opsdeck.improvement.gaps import Gap, find_gaps, unmatched
from opsdeck.improvement.job import run_job
from opsdeck.improvement.learned import (
    LearnedPhrase,
    Similarity,
    learn,
    load_redactions,
    read_learned,
    revert,
    write_learned,
)
from opsdeck.improvement.report import (
    DraftedProposal,
    ImprovementReport,
    read_report,
    render_json,
    render_text,
    write_report,
)
from opsdeck.improvement.signals import TurnSignal, load_signals, read_events
from opsdeck.improvement.tuning import Tuning, rank_cases, tune, write_overrides

__all__ = [
    "TIE",
    "DraftedProposal",
    "EvaluationReport",
    "Gap",
    "ImprovementReport",
    "LearnedPhrase",
    "RoutingCase",
    "Similarity",
    "TurnSignal",
    "Tuning",
    "find_gaps",
    "learn",
    "load_cases",
    "load_redactions",
    "load_signals",
    "rank_cases",
    "read_events",
    "read_learned",
    "read_report",
    "render_json",
    "render_text",
    "revert",
    "run_evaluation",
    "run_job",
    "tune",
    "unmatched",
    "write_learned",
    "write_overrides",
    "write_report",
]
