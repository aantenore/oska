# 09 Evolution layer (L7)

## Purpose

L7 is how the catalogue grows. A session does something useful that no
procedure covers; the system drafts a candidate procedure from what actually
ran; a quality gate checks it against the live catalogue; a person moves it
through a lifecycle; publication writes a new version into a catalogue root and
the runtime picks it up.

The layer exists to make learning safe rather than to make it fast. Its whole
design is the gap between "the system produced a procedure" and "the system can
run it". Nothing closes that gap except a named human.

## The autonomy ladder

A system that learns from its own traffic has to state, once and in public, how
far it may go without a person. The statement is the deliverable of this layer.
Not the cleverness of the job that improves things: the boundary, declared,
testable, and impossible to cross from inside the code.

| Level | What it does | What it may touch | What moves it |
| --- | --- | --- | --- |
| 0 | Observes. Reads back what a run left behind. | Nothing. The audit trail and the write ledger are read, never written. | Nothing; observation has no effect. |
| 1 | Changes what is cheap and reversible, and shows every change. | Learned phrasings, one file per procedure under the state directory, which the index ranks by; an overrides file holding two thresholds nothing reads; the report. | The job, on its own, for the phrasings; a person, for the thresholds, which the job proposes and never applies. |
| 2 | Drafts what it cannot do, and stops. | The proposal store, in `draft` and in no other state. | The job writes the draft; nothing else. |
| 3 | Never acts alone. | Proposal states beyond `draft`, policies, connector bindings, publication. | A named person, through the lifecycle of this layer. |

Level 0 is the ground. Every number the layer reports comes from the record two
other layers already keep for their own reasons: the audit trail says what was
asked, where it was routed, what discovery scored it and which operations ran;
the write ledger says whether a write was confirmed by its read-back. Nothing is
recorded to make improvement possible, and nothing improvement does is written
back into either.

Level 1 is what the system may change by itself, and one of its two parts is
smaller than it looks. Phrasings learned beside a procedure are a change the job
makes: the index reads them alongside the declared triggers and the derived
phrases, so a request in the words people really use reaches the procedure it
means. That change is real, it is visible in a ranking, and it is undone by
deleting one file — the fingerprint the index holds covers those files, so the
deletion is followed by a rebuild and the ranking that was there before.

The two thresholds discovery decides with are not a change the job makes. It
searches for a better pair inside declared bounds and writes what it found to an
overrides file under the state directory, next to the pair in force and the
refusals. Nothing reads that file: applying it is a person editing the runtime
configuration. The job proposes; it does not move a threshold.

Level 2 is where competence ends and judgement begins. A cluster of requests
that reached no procedure is evidence that something is missing. It is not
evidence of what should be built, and the system has no standing to decide that.
It drafts a candidate, in `draft`, and stops.

Level 3 is the list of things that never happen without a person: moving a
proposal past `draft`, publishing anything that can write, changing a policy,
changing a connector binding. The list is short on purpose. Everything on it
either grants a capability or changes the rules a capability runs under.

One machine-written artefact changes what the runtime does and is not on the
ladder at all: the routing examples publication derives from the declared
triggers, written as a file inside the skill's own version directory, which the
index ranks by. It is outside the ladder because it is not something the system
does to itself while it runs. It is produced at publication, under a person's
decision to publish that version, from the triggers that person is answerable
for; it is thrown away and reproduced rather than maintained, and it is ignored
in full the moment the triggers it was derived from change. The ladder governs
what a scheduled job may do between publications, and nothing on it writes
inside a catalogue root.

## Modules

### Proposal

Responsibility: hold a candidate skill and everything known about it.

```text
ProposalState: draft | submitted | approved | rejected | published

SkillProposal:
  proposal_id: string
  state: ProposalState
  source_session_id: string
  skill_markdown: string          # the candidate artefact, frontmatter and body
  policy: object | null
  quality: QualityReport
  history: [{state, at, by, note}]
```

A proposal is drafted from a session or written by hand. Drafting from a
session reads what the session actually did — the operations executed, the
receipts recorded, the entities touched — and turns that into a candidate:
identifier, description, triggers, entities, integrations, capabilities split
into reads and writes, runtime wording, and a policy skeleton covering the
writes. With a model provider the body sections are written from the
transcript; without one, from a template. The capabilities are derived from
receipts and reads, never invented.

```text
ProposalStore:
  save(proposal) -> path
  get(proposal_id) -> SkillProposal
  list(state) -> [SkillProposal]

draft_from_session(state, catalogue, provider | null) -> SkillProposal
```

### Quality Gate

Responsibility: decide whether a candidate is fit to be looked at, and say why
not.

It parses the candidate, validates it against the skill schema and the live
catalogue, and runs lints that a schema cannot express: the description is
within length and is not a restatement of the identifier; there are at least
three distinct triggers and they do not duplicate the triggers of a published
procedure; every write is declared by a listed integration; a procedure that
writes carries a policy; the verification section is not empty.

```text
QualityReport: {issues: [string], score: 0..1}
check(markdown, catalogue) -> QualityReport
```

The report travels with the proposal. A refused publication keeps its report,
so the reason survives.

### Lifecycle

Responsibility: move a proposal between states, and record who did it.

```text
transition(proposal, to_state, by, note) -> SkillProposal
```

Allowed edges: `draft -> submitted`, `submitted -> approved`,
`approved -> published`, and any state to `rejected`. Every transition carries
an actor and is appended to the history. There is no transition that the system
performs on its own, and none that skips a state.

### Publication

Responsibility: write an approved proposal into a catalogue root as a new
version, and nothing else.

Publication computes the next version for the identifier, writes the skill file
and its policy into the configured publish root under that version, and
re-validates against the live catalogue before committing. Any validation issue
refuses the publication and keeps the report. The runtime picks the new version
up on its next fingerprint check; no restart, and no other layer is touched.

### Signals

Responsibility: read one run back, one turn at a time.

```text
TurnSignal:
  session_id, turn_id: string
  text: string                    # what was asked
  language: string
  route_source: string
  chosen_runbook: string | null   # the procedure the turn reached
  score: float                    # what the top candidate scored
  tie: boolean
  outcomes: [string]              # the operations that completed
  verified_write: boolean         # the ledger confirmed the read-back
  at: timestamp

load_signals(audit_path, ledger) -> [TurnSignal]
```

Ground truth for routing is free and nobody has to label it. A turn whose write
was verified, or whose reads completed, names the procedure the request actually
meant, next to the words the person actually used. That pair is the whole
signal, and it is what makes level 1 possible at all: without it, the system
would be tuning itself against its own guesses.

Where the trail and the ledger disagree, the ledger decides. An audit line
saying a write was verified is a claim made while the turn was running; the
ledger holds what the read-back found.

### Learned phrasings

Responsibility: keep the words people really used, beside a procedure and never
inside it.

```text
learn(signals, catalogue, config) -> {skill_id: [LearnedPhrase]}
write_learned(root, skill_id, phrases) -> path
revert(root, skill_id) -> boolean
```

A phrase is kept only from a turn that reached a procedure and finished. It is
normalised, redacted with patterns read from a file, and dropped when it says
nothing the declared triggers or the phrases already kept do not say. What
survives is written to `<learned root>/<skill id>/learned-examples.json` with
the moment it was written, capped by configuration.

These files are runtime state, never authored truth. They sit under the state
directory, not in a catalogue root, and `revert` deletes one.

What is written is what the index reads. A procedure is ranked under its
declared texts first, then the phrases derived from its triggers at publish
time, then these, all three under one cap per procedure that never cuts a
declared text; a ranking names which of the three it matched on, so a route won
by a learned phrase says so. The registry fingerprints the skill files alone, so
an index that ranks by these files folds them into the fingerprint it holds:
writing one rebuilds the index, and so does deleting one. That is what makes
`revert` a complete undo rather than a tidy-up — it is a deletion and a rebuild,
and the ranking afterwards is the ranking from before.

### Threshold tuning

Responsibility: propose two numbers, inside bounds, and never apply them.

```text
Tuning: {before, after, decisive_rate, precision, refused: [string]}
tune(cases, rankings, discovery_config, bounds) -> Tuning
```

The search reads the floor a candidate has to clear and the margin under which a
runner-up makes the top a tie, and looks for a better pair. It maximises the
cases answered correctly subject to answering none of them wrongly, and it
refuses any value outside the bounds the configuration declares, naming the
refusal in the result. The result is written to an overrides file under the
state directory. No part of the runtime reads that file: it is a proposal, and
applying it is a person changing the runtime configuration on purpose. The
thresholds in force are not touched by the job under any flag.

### Routing evaluation

Responsibility: say how routing is doing, over a set a person wrote.

```text
RoutingCase: {text, expected, language}    # expected is a skill id, or "tie"
EvaluationReport:
  {precision, decisive_rate, model_pick_rate, per_language, failures}
run_evaluation(cases, discovery) -> EvaluationReport
```

The set is written by hand, in more than one language, in wording that is not a
copy of the declared triggers, and some of its cases are ambiguous on purpose
and expect a tie. It is routed over the catalogue as the runtime ranks it —
derived phrases and the phrasings just learned included — so a phrase that
spoils a route shows up in the same report that names it, and the thresholds are
searched over that ranking rather than over a catalogue nobody runs. `precision` is measured over the cases the system answered at
all: declining to answer is an abstention, and abstention is a design position
of this architecture, not a failure to be optimised away.

### Gaps

Responsibility: count what reached nothing, and describe it in declared words.

```text
Gap: {size, sample_phrases, suggested_entities, suggested_operations}
find_gaps(signals, ontology, config) -> [Gap]
```

Requests that reached no procedure are clustered, and a cluster is reported only
when it is large enough to be a pattern. What it suggests comes from the
ontology: entities already declared, and operations already bound to them. A gap
never suggests a written procedure.

### The improvement job

Responsibility: run the four steps, write one report, and stay under the
ceiling.

```text
run_job(settings, provider, apply_tuning) -> ImprovementReport
render_text(report) / render_json(report)
```

The report names every phrase kept, every threshold moved, every cluster
counted, every candidate drafted, and the precision of the previous run it is
being compared against. A change nobody can see is not a reversible change; it
is a change that happens to be small.

## Invariants

- No automatic approval. No automatic publication.
- Every state transition records a named actor and a timestamp.
- A proposal may not skip a state; the only shortcut is rejection.
- Publication writes into a catalogue root and nowhere else. L7 does not touch
  governance, sessions, connectors, the ledger or the audit sink other than to
  emit its own events.
- Publication re-validates against the live catalogue and refuses on any issue.
- A published proposal becomes an ordinary skill, governed like any other. It
  receives no privilege from having been learned.
- A refused publication keeps its quality report.
- Capabilities in a drafted proposal are derived from what the session actually
  executed.
- The improvement job declares an autonomy level for everything it does, and
  does nothing above it.
- Signals are read from the audit trail and the write ledger and written back to
  neither.
- A learned phrasing is a file beside a procedure, not an edit to it, and
  deleting the file undoes it completely, its effect on ranking included.
- A learned phrasing is ranked after the declared texts and the derived phrases,
  under the same cap, and a ranking says which of the three it matched on.
- Tuning refuses any value outside the declared bounds and writes to an
  overrides file the runtime does not read, never to the runtime configuration.
  Applying it is a person's change.
- Evaluation is an alarm. A drop is reported to a person and reverts nothing.
- A gap suggests entities and operations the ontology already declares, and
  never a written procedure.
- Nothing in the improvement job reaches `submitted`, `approved` or `published`,
  writes a policy, or edits a connector configuration.

## Requirements

- **OSKA-EVO-1** — A proposal drafted from a session MUST derive its
  capabilities from operations that session actually executed, and MUST NOT
  declare a capability the session did not exercise.
- **OSKA-EVO-2** — Every proposal MUST carry a quality report produced by
  validating the candidate against the live catalogue.
- **OSKA-EVO-3** — The lifecycle MUST permit only the transitions
  `draft -> submitted`, `submitted -> approved`, `approved -> published` and
  `any -> rejected`; any other transition MUST be refused.
- **OSKA-EVO-4** — Every transition MUST record an actor and a timestamp in the
  proposal history.
- **OSKA-EVO-5** — There MUST be no code path that reaches `approved` or
  `published` without an explicit call carrying an actor.
- **OSKA-EVO-6** — Publication MUST re-validate the candidate against the live
  catalogue and MUST refuse on any validation issue, keeping the report.
- **OSKA-EVO-7** — Publication MUST write a new version under the configured
  catalogue root and MUST NOT overwrite an existing version.
- **OSKA-EVO-8** — A published skill MUST be picked up by the catalogue without
  restarting the runtime.
- **OSKA-EVO-9** — A published skill MUST be subject to the same validation,
  policy and governance as a hand-written one, with no exemption for its
  origin.

- **OSKA-EVO-10** — The evolution layer MUST declare the autonomy level of every
  change it makes, and MUST NOT make a change above the level it declares.
- **OSKA-EVO-11** — Signals MUST be derived from the audit trail and the write
  ledger only, and reading them MUST NOT write to either. Where the two
  disagree about a write, the ledger MUST decide.
- **OSKA-EVO-12** — A learned phrasing MUST be written to a file separate from
  the skill it belongs to, MUST NOT alter any authored skill, and MUST be
  undone completely by deleting that file.
- **OSKA-EVO-13** — Learned phrasings MUST be limited per procedure by
  configuration, MUST be redacted using patterns read from a file rather than
  written into logic, and MUST NOT be kept from a turn that reached no
  procedure or whose operations did not complete.
- **OSKA-EVO-13a** — Learned phrasings MUST be among the texts an index is built
  from, after the declared texts and the derived phrases and under the same cap
  per procedure, and an index MUST rebuild when one of their files is written or
  deleted, so that reverting restores the previous ranking.
- **OSKA-EVO-14** — Threshold tuning MUST refuse any value outside the bounds
  the configuration declares, MUST report each refusal, and MUST NOT write into
  the runtime configuration. The overrides file it writes MUST be a proposal:
  no code path may apply it, and the thresholds in force MUST change only by a
  person changing the configuration.
- **OSKA-EVO-15** — Tuning MUST NOT select a configuration that decides any case
  of the labelled set wrongly.
- **OSKA-EVO-16** — Routing evaluation MUST run against a labelled set written
  by hand, in more than one language, containing cases whose expected answer is
  a tie.
- **OSKA-EVO-17** — A drop in evaluation precision MUST be reported, and MUST
  NOT cause anything to be reverted, retuned or republished automatically.
- **OSKA-EVO-18** — A gap MUST be reported only for a cluster of at least the
  configured size, its suggestions MUST come from the declared ontology, and it
  MUST NOT contain a written procedure.
- **OSKA-EVO-19** — A candidate drafted by the improvement job MUST be in
  `draft`, and no code path of that job may move it out of `draft`. This MUST be
  proved by running the job against a store that refuses any other state, and
  not only by reading its source.
- **OSKA-EVO-20** — The improvement job MUST NOT write a policy, MUST NOT alter
  a connector configuration, and MUST NOT publish a skill.
- **OSKA-EVO-21** — Every change the improvement job makes MUST appear in the
  report it writes, and that report MUST record the precision of the previous
  run it is compared against.

## Rationale

The obvious alternative is self-improvement: let the system notice a repeated
sequence, write the procedure, and start using it. Every argument for it is an
argument about speed, and every argument against it is the same argument as the
approval gate one layer down. A write is one irreversible act; granting a new
capability is the other, and it is worse, because a capability is a write that
will happen many times to records nobody has looked at yet. If a person must
approve a single restart, a person must approve the procedure that can restart
anything.

The second alternative is to skip learning entirely and let people author every
skill by hand. That is safe and it wastes the one thing the runtime has that
the author does not: a record of what actually ran. A session that solved a
problem holds the operations, the payload shapes, the entities and the order.
Drafting from receipts rather than from the conversation is what makes the
candidate worth reviewing — it declares the capabilities that were used, not the
capabilities that were discussed. The reviewer's job becomes judgement about
whether this should be a standing capability, not archaeology about what
happened.

The lifecycle is four states because three of them answer different questions.
`submitted` says an author is done. `approved` says a reviewer agrees the
procedure should exist. `published` says it validated against the catalogue as
it is now, which is a separate fact and can fail after approval — the catalogue
moves. Collapsing approval and publication would make a stale approval publish
an invalid skill, so `OSKA-EVO-6` re-validates at the last possible moment and
refuses rather than publishing something the validator would reject on the next
load.

### Why the ladder is the deliverable

Self-improvement is not one decision, it is a slope, and every point on it is
defensible next to the point above it. A system that keeps learned phrasings is
obviously safer than one that edits skills; one that edits skills is obviously
safer than one that publishes them; one that publishes is obviously safer than
one that also writes the policy. The argument never stops, because each step is
small. What stops it is naming the levels, saying which one this system stands
on, and making the next one unreachable by a code path rather than by intent.

What proves the ceiling is a run, not a reading. `test_improvement.py` runs the
job against a proposal store that raises on any proposal handed to it in a state
other than `draft`, inside a guard that raises on any write outside the state
directory, and compares the catalogue, the configuration, the schemas and this
specification before and after; the job has to finish. A scan of the syntax tree
stands behind it as a cheap second line, failing on a call that names a state
beyond `draft` and on an import of the lifecycle, a connector or the policy
engine. The scan sees only what is written, and the state of a proposal can be
set without calling anything: the run catches that, and the scan does not.

### Why learned phrasings are files and not edits

A learned phrasing could be appended to the triggers of the skill it belongs to.
It would rank identically and cost one fewer file. It is refused for two
reasons. An edit cannot be undone by deleting anything: undoing it means knowing
which lines were authored and which were learned, which is exactly the
distinction the edit destroyed. And an authored skill is a document a person is
answerable for; a person who finds sentences in it that nobody wrote stops
trusting the rest. A separate file keeps the authored artefact authored, and
makes the whole of level 1 revertible by `rm`.

### Why tuning is bounded

An unbounded search over the routing thresholds finds the same answer every
time: a floor of zero and a margin of zero, which answer every case decisively
and never decline. The metric improves and the system gets worse, because
declining is what it does instead of guessing at a write. The bounds are a
person's statement of how far the numbers may drift from the ones they chose.
A better value outside them is not taken quietly; it is refused, and the
refusal is reported, so the next move is a person widening the bounds on
purpose rather than a job widening them by arithmetic.

### Why evaluation is an alarm and not a trigger

It is tempting to let a drop in precision revert whatever caused it. That turns
a measurement into a control loop, and control loops built on a metric optimise
the metric. Worse, the thing that moved is usually not the thing to revert: a
new procedure legitimately takes cases from an old one, and a changed trigger
legitimately changes a score. The report says precision fell, which cases were
missed, and what it is being compared against. A person decides whether the
catalogue got worse or the labelled set got old.

## In the reference implementation

- `src/opsdeck/learning/` — `proposals.py` (store and drafting), `quality.py`
  (lints and report), `lifecycle.py` (transitions and publication).
- `src/opsdeck/contracts/proposal.py` — the proposal, its states and its
  history.
- `schemas/proposal.schema.json` — the normative proposal contract.
- `configs/runtime.json` — the publish root under `learning`.
- `src/opsdeck/cli/commands_propose.py` and the `/proposals` routes of
  `src/opsdeck/api/routes.py` — the two surfaces that draft a candidate and
  move it, both calling the same `transition` with the actor the caller named.
- `examples/ops/scenarios/15_learning_lifecycle.json` — the lifecycle as an
  executable document: a session, a drafted candidate, a move without an actor
  that is refused, and a publication the catalogue picks up.
- `src/opsdeck/improvement/` — `signals.py` (the run, read back), `learned.py`
  (phrasings kept beside a procedure, and reverted), `tuning.py` (bounded
  threshold search and the overrides file), `evaluation.py` (the labelled set
  and its report), `gaps.py` (clusters and ontology suggestions), `job.py` and
  `report.py` (the four steps and what they wrote).
- `src/opsdeck/cli/commands_improve.py` — `improve run`, `improve show`,
  `improve revert` and `improve evaluate`; a run whose precision is below the
  previous report leaves a non-zero status and changes nothing.
- `configs/runtime.json` — the improvement root, the report path, the caps and
  the bounds under `improvement`.
- `examples/ops/evaluation/routing.json` — the labelled routing set, in English
  and Italian, with two cases that expect a tie.
- `examples/ops/evaluation/redactions.json` — the patterns applied to a learned
  phrasing before it is written.
- `src/opsdeck/index/base.py` — where the learned files are read back and
  fingerprinted, and where a ranking is labelled declared, derived or learned;
  `src/opsdeck/engine/factory.py` wires them into the two layers that rank the
  catalogue.
- `tests/unit/test_improvement.py` — the ladder as an executable document: the
  run against a store that refuses anything but a draft, which is what proves
  the ceiling, the scan of the syntax tree behind it, and the revert that gives
  the previous ranking back.
