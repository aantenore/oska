"""The autonomy ladder: how far this system may improve itself without a person.

A system that learns from its own traffic has to say, once and in public, where
it stops. This package is that statement, written as code rather than as an
intention.

Level 0 — observe. Signals are read back from the audit trail and the write
ledger, and a judgment a person recorded against a turn is read with them.
Recording one changes nothing else.

Level 1 — adjust what is cheap and reversible, and show every change. Learned
phrasings are written as separate files under the improvement root, one per
procedure, each revertible by deleting it. A preference about how answers read,
classified from the comment of a judgment at job time, is written the same way
and bounded the same way: it may change how an answer reads and may never
change what the runtime does. Threshold tuning is proposed as an overrides file
a caller may apply. Nothing authored is edited.

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
from opsdeck.improvement.feedback import (
    FeedbackStore,
    TurnFeedback,
    feedback_root,
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
from opsdeck.improvement.preferences import (
    Preference,
    classify_comment,
    learn_preferences,
    preferences_root,
    read_preferences,
    revert_preferences,
    write_preferences,
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
    "FeedbackStore",
    "Gap",
    "ImprovementReport",
    "LearnedPhrase",
    "Preference",
    "RoutingCase",
    "Similarity",
    "TurnFeedback",
    "TurnSignal",
    "Tuning",
    "classify_comment",
    "feedback_root",
    "find_gaps",
    "learn",
    "learn_preferences",
    "load_cases",
    "load_redactions",
    "load_signals",
    "preferences_root",
    "rank_cases",
    "read_events",
    "read_learned",
    "read_preferences",
    "read_report",
    "render_json",
    "render_text",
    "revert",
    "revert_preferences",
    "run_evaluation",
    "run_job",
    "tune",
    "unmatched",
    "write_learned",
    "write_overrides",
    "write_preferences",
    "write_report",
]
