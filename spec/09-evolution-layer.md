# 09 Evolution layer (L7)

## Purpose

L7 is how the catalog grows. A session does something useful that no
procedure covers; the system drafts a candidate procedure from what actually
ran; a quality gate checks it against the live catalog; a person moves it
through a lifecycle; publication writes a new version into a catalog root and
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
| 0 | Observes. Reads back what a run left behind. | Nothing. The audit trail, the write ledger and the judgments people recorded are read, never written. | Nothing; observation has no effect. |
| 1 | Changes what is cheap and reversible, and shows every change. | Learned phrasings, one file per procedure under the state directory, which the index ranks by; preferences about how answers read, one file per session, which only the reply writer sees; an overrides file holding two thresholds nothing reads; the report. | The job, on its own, for the phrasings and the preferences; a person, for the thresholds, which the job proposes and never applies. |
| 2 | Drafts what it cannot do, and stops. | The proposal store, in `draft` and in no other state. | The job writes the draft; nothing else. |
| 3 | Never acts alone. | Proposal states beyond `draft`, policies, connector bindings, publication. | A named person, through the lifecycle of this layer. |

Level 0 is the ground. Every number the layer reports comes from the record two
other layers already keep for their own reasons: the audit trail says what was
asked, where it was routed, what discovery scored it and which operations ran;
the write ledger says whether a write was confirmed by its read-back. Nothing is
recorded to make improvement possible, and nothing improvement does is written
back into either.

The one thing that record cannot hold is whether the answer was any good, so
level 0 also reads the judgments people recorded against turns. A judgment is
written by a person asking for it to be written, changes nothing else, and is
read here exactly as the trail is: as evidence, not as an instruction.

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

Level 2 is where competence ends and judgment begins. A cluster of requests
that reached no procedure is evidence that something is missing. It is not
evidence of what should be built, and the system has no standing to decide that.
It drafts a candidate, in `draft`, and stops.

Level 3 is the list of things that never happen without a person: moving a
proposal past `draft`, publishing anything that can write, changing a policy,
changing a connector binding. The list is short on purpose. Everything on it
either grants a capability or changes the rules a capability runs under.

Level 3 used to be a promise, and it is now a shape. A promise is a statement
that the code will not do something it is able to do; the only thing holding it
is that nobody wrote the call. That is enough for a review and not enough for a
deployment, because the distance between "nothing calls it" and "nothing can
call it" is one well-meaning patch. What makes level 3 structural is that the
process answering people has no write path into the catalog it answers from.
Publication does not write a skill file. It composes a change request, and the
one function that installs one takes the catalog root as an argument from its
caller — there is no configured default it could fall back to, so a caller that
has no root to give cannot install anything. The engine has no root to give.
The HTTP surface has no root to give. A person at a terminal does, and types
it.

One machine-written artifact changes what the runtime does and is not on the
ladder at all: the routing examples publication derives from the declared
triggers, written as a file inside the skill's own version directory, which the
index ranks by. It is outside the ladder because it is not something the system
does to itself while it runs. It is produced at publication, under a person's
decision to publish that version, from the triggers that person is answerable
for; it is thrown away and reproduced rather than maintained, and it is ignored
in full the moment the triggers it was derived from change. The ladder governs
what a scheduled job may do between publications, and nothing on it writes
inside a catalog root.

## Modules

### Proposal

Responsibility: hold a candidate skill and everything known about it.

```text
ProposalState: draft | submitted | approved | rejected | published

SkillProposal:
  proposal_id: string
  state: ProposalState
  source_session_id: string
  skill_markdown: string          # the candidate artifact, frontmatter and body
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
  save(proposal)
  get(proposal_id) -> SkillProposal
  list(state) -> [SkillProposal]
  delete(proposal_id)

draft_from_session(state, catalog, provider | null) -> SkillProposal
```

### Quality Gate

Responsibility: decide whether a candidate is fit to be looked at, and say why
not.

It parses the candidate, validates it against the skill schema and the live
catalog, and runs lints that a schema cannot express: the description is
within length and is not a restatement of the identifier; there are at least
three distinct triggers and they do not duplicate the triggers of a published
procedure; every write is declared by a listed integration; a procedure that
writes carries a policy; the verification section is not empty.

```text
QualityReport: {issues: [string], score: 0..1}
check(markdown, catalog) -> QualityReport
```

The report travels with the proposal. A refused publication keeps its report,
so the reason survives.

The gate also runs what the skill under review says about itself. A skill that
carries an evaluation set (`OSKA-SKL-25`) carries the requests its author said
must reach it and the requests that must not, and a candidate whose own cases
fail is not fit to be looked at: the author has said what the procedure is for,
and the ranking disagrees. This runs where a ranking is available, because the
gate ranks the way the runtime ranks rather than scoring on its own. A skill
that carries no set is not refused for that; an absent set is a set nobody
wrote, not a failing one.

### Lifecycle

Responsibility: move a proposal between states, and record who did it.

```text
transition(proposal, to_state, by, note) -> SkillProposal
```

Allowed edges: `draft -> submitted`, `submitted -> approved`,
`approved -> published`, and any state to `rejected`. Every transition carries
an actor and is appended to the history, with where that actor came from: the
principal of the call, the operator at a terminal, or a name the call only
stated, which is recorded as unverified (`OSKA-GOV-48` in
[06-governance-layer.md](06-governance-layer.md)). There is no transition that
the system performs on its own, and none that skips a state.

### Publication

Responsibility: turn an approved proposal into a reviewable artifact, and
nothing else.

Publication re-validates against the live catalog and refuses on any issue,
exactly as it always did. What it then produces has changed. It computes the
next version for the identifier, renders the skill file that version would
carry, and writes that file and its policy into a change request under the
configured change request root. Nothing is written under a catalog root, and
the function that publishes takes no catalog root to write one with.

The lifecycle is unchanged in meaning: observe, draft, quality gate, human
decision. Only the effect of the last step has moved. `published` still means
that a named person approved this text and that it validated against the
catalog as it stood at that moment. It no longer means that the runtime
started serving it.

### Change request

Responsibility: carry one proposed catalog change from the process that
composed it to the person who decides on it.

A change request is a directory named by a request id. It holds the skill
package laid out exactly as a catalog root is laid out, so installing it is a
copy of one subtree and not a translation; a record naming the request, the
proposal it came from, the skill, the version, who asked, when, the quality
report and the state of the request; and a summary in prose saying what changes
and who asked for it. Routing examples derived at publication are written inside
the package, so they travel with it and arrive only if the request does.

A request is `open` until it is decided, and it is decided once. `apply` copies
the package into a catalog root its caller names, refuses to overwrite a
version that is already there, and records who applied it and where. `reject`
closes it without copying anything and records who and why. Both are acts of a
person, both leave a record, and neither is reachable from the serving process:
the catalog root is an argument with no default, and nothing in the engine,
the HTTP application or a scheduled job has one to pass.

Applying is not serving. A catalog root that has gained a version is a source;
what a runtime answers from is a revision built from that source. The command
that applies says so, and names the build step, so the two are never confused.

### Publication as a branch

Responsibility: put a change request in front of its reviewer where the
catalog's sources already live.

A catalog kept in a version-controlled repository is reviewed there, not in a
directory under the state root. A publisher takes a composed change request and
puts it in front of a person; which publisher is used is a choice, and the
change request root remains the record every decision is read from.

```text
ChangePublisher:
  name: string
  publish(request, store) -> request       # records how it was published

PullRequestOpener:
  open(branch, title, cwd) -> answer       # what the hosting provider said
```

`directory` is the default and writes nothing beyond the change request. `git`
writes the package into a new branch of a repository the operator names, under
the catalog path the operator names, as one commit authored by the actor who
approved the publication. The branch is made in a worktree of its own, so the
checkout a person works in is never touched. A version the base already holds
is refused, as `apply` refuses it. The branch and the commit are recorded on the
change request, which stays open: merging the branch is the review, and it is
a person's act in the repository, not the runtime's.

Nothing is pushed unless the caller asks for it, and nothing is ever forced. A
pull request is a hosting provider's notion and stays outside the runtime: the
operator configures a command, the branch and the title are substituted into
its arguments, and it runs without a shell. A branch that cannot be made
during a transition does not undo the transition; the note says so, and the
request can be published again from a terminal.

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
  turn: integer                   # the position of the turn in its session
  feedback: TurnFeedback | null   # what a person thought of the answer
  at: timestamp

load_signals(audit_path, ledger, feedback) -> [TurnSignal]
```

Ground truth for routing is free and nobody has to label it. A turn whose write
was verified, or whose reads completed, names the procedure the request actually
meant, next to the words the person actually used. That pair is the whole
signal, and it is what makes level 1 possible at all: without it, the system
would be tuning itself against its own guesses.

Where the trail and the ledger disagree, the ledger decides. An audit line
saying a write was verified is a claim made while the turn was running; the
ledger holds what the read-back found.

### A judgment on an answer

Responsibility: record what a person thought of one answer, and nothing else.

```text
TurnFeedback:
  session_id: string
  turn: integer                   # counted from 1, as a person sees it
  score: up | down
  comment: string                 # optional, in the words of the person
  at: timestamp
```

What the record of a run cannot show is whether an answer was any good. The
trail says what was asked, where it went and what ran; none of that is a
judgment, and until someone is asked the system never finds out. So the
addition is the smallest honest one: a score against one turn of one session,
with an optional comment.

Recording one changes nothing by itself. It writes one line under the state
directory. No procedure changes, no threshold moves, no wording is learned
because of it. What reads it decides what it is worth, and what a signal
carries is the last judgment recorded against its turn.

A turn is named by its position in the session, because that is the number a
person can quote. The identifier the audit trail gives a turn is a random one
nobody sees.

### Presentation preferences

Responsibility: keep what a person asked for about how answers read, and keep
it away from everything else.

```text
Preference:
  text: string                    # one short instruction about how to write
  session_id: string
  turn: integer
  at: timestamp
```

A judgment of `down` sometimes carries a comment that is not about the answer
at all but about the shape of it: too long, no lists, greet me first. That is
worth keeping, on a very short leash.

The leash is one sentence. A preference may change how an answer reads. It may
never change what the runtime does. It reaches the reply writer as a fact and
reaches nothing else: not the planner, not a policy, not a payload, not a
routing decision. A comment the classification reads as a request to do
something is not a preference about reading, and is discarded rather than
stored, because a stored one would be a standing instruction to act that nobody
approved.

Classification happens when the job runs and never during a turn. A turn that
had to decide what a comment meant would be spending a model call nobody asked
it for, and would be acting on a sentence before anyone had read it. Where the
classification is uncertain, the answer is that there is no preference.

A preference is a file under the state directory, like a learned phrasing, and
deleting the file is the whole of the undo.

### Learned phrasings

Responsibility: keep the words people really used, beside a procedure and never
inside it.

```text
learn(signals, catalog, config) -> {skill_id: [LearnedPhrase]}
write_learned(root, skill_id, phrases) -> path
revert(root, skill_id) -> boolean
```

A phrase is kept only from a turn that reached a procedure and finished. It is
normalized, redacted with patterns read from a file, and dropped when it says
nothing the declared triggers or the phrases already kept do not say. What
survives is written to `<learned root>/<skill id>/learned-examples.json` with
the moment it was written, capped by configuration.

These files are runtime state, never authored truth. They sit under the state
directory, not in a catalog root, and `revert` deletes one.

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
runner-up makes the top a tie, and looks for a better pair. It maximizes the
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
and expect a tie. It is routed over the catalog as the runtime ranks it —
derived phrases and the phrasings just learned included — so a phrase that
spoils a route shows up in the same report that names it, and the thresholds are
searched over that ranking rather than over a catalog nobody runs. `precision` is measured over the cases the system answered at
all: declining to answer is an abstention, and abstention is a design position
of this architecture, not a failure to be optimized away.

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
- The process that answers people has no write path into the catalog it
  answers from. Publication writes a change request under the change request
  root and nowhere else. L7 does not touch governance, sessions, connectors,
  the ledger or the audit sink other than to emit its own events.
- Installing a change request takes the catalog root from its caller. There is
  no configured default, so a caller with no root cannot install anything.
- A change request is decided once, never overwrites an existing version, and
  records who decided it.
- No HTTP operation applies a change request. The surface reads them.
- A change request published as a branch stays open. The runtime never pushes
  unless asked, never forces, and never merges.
- Publication re-validates against the live catalog and refuses on any issue.
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
  validating the candidate against the live catalog.
- **OSKA-EVO-3** — The lifecycle MUST permit only the transitions
  `draft -> submitted`, `submitted -> approved`, `approved -> published` and
  `any -> rejected`; any other transition MUST be refused.
- **OSKA-EVO-4** — Every transition MUST record an actor and a timestamp in the
  proposal history.
- **OSKA-EVO-5** — There MUST be no code path that reaches `approved` or
  `published` without an explicit call carrying an actor.
- **OSKA-EVO-6** — Publication MUST re-validate the candidate against the live
  catalog and MUST refuse on any validation issue, keeping the report.
- **OSKA-EVO-7** — Publication MUST write a new version under the configured
  catalog root and MUST NOT overwrite an existing version. *Superseded by
  `OSKA-EVO-22`: publication composes a change request, and it is the applying
  of that request that writes a new version and never overwrites one.*
- **OSKA-EVO-8** — A published skill MUST be picked up by the catalog without
  restarting the runtime. *Read with `OSKA-EVO-22`: this holds of a skill whose
  change request has been applied to a catalog root the runtime reads.*
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
  of the labeled set wrongly.
- **OSKA-EVO-16** — Routing evaluation MUST run against a labeled set written
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

- **OSKA-EVO-22** — A process that serves a catalog MUST NOT create or modify
  any file under a catalog root. Reaching `published` MUST compose a change
  request under the configured change request root, and the function that
  performs the transition MUST NOT accept a catalog root. This supersedes the
  writing half of `OSKA-EVO-7`.
- **OSKA-EVO-23** — A change request MUST hold the skill package exactly as it
  would be published, laid out as a catalog root is laid out
  (`<type>/<name>/v<N>/skill.md` and the policy it declares); a record naming
  the request, the proposal it came from, the skill, the version, the actor,
  the moment it was composed, the quality report and the state of the request;
  and a summary in prose naming what changes and who asked for it.
- **OSKA-EVO-24** — Installing a change request MUST take the catalog root as
  an argument from its caller. No configured value may stand in for it, and no
  HTTP endpoint, engine path or scheduled job may reach it.
- **OSKA-EVO-25** — Installing a change request MUST NOT overwrite an existing
  version, MUST record who installed it and into which root, and a request MUST
  be decided at most once. Rejecting one MUST copy nothing and MUST record who
  rejected it.
- **OSKA-EVO-26** — `OSKA-EVO-22` MUST be proved by a run and not by reading the
  source: a turn, a proposal drafted from it and a full lifecycle to `published`
  MUST all succeed against a catalog root that raises on any write, and a hash
  of that root MUST be identical before and after.
- **OSKA-EVO-27** — The HTTP surface MUST offer no operation that installs a
  change request. It MUST offer reading one and reading the list of them, and
  the transition answer MUST carry the request the transition composed.
- **OSKA-EVO-28** — Reaching `published` MUST record on the proposal which
  change request it composed, so the artifact a decision produced can be found
  from the decision.
- **OSKA-EVO-29** — A conformant system MUST offer a way to record a judgment
  on one answer: a score of up or down against a turn of a session, with an
  optional comment. Recording one MUST change nothing else, and MUST NOT be a
  level 1 change.
- **OSKA-EVO-30** — A judgment MUST be stored under the state directory and
  MUST be readable as a level 0 signal of the turn it names. A judgment naming
  a turn the record does not hold MUST NOT become a signal.
- **OSKA-EVO-31** — A comment carried by a judgment MAY be classified as a
  preference about how answers read. That classification MUST happen when the
  improvement job runs and MUST NOT happen during a turn. A comment classified
  as a request to do something MUST be discarded and MUST NOT be stored.
- **OSKA-EVO-32** — A preference recorded from such a comment MUST reach the
  reply writer and MUST NOT reach the planner, a policy or an operation
  payload. It MUST NOT change which procedure a request routes to, what a plan
  contains, or what a policy decides.
- **OSKA-EVO-33** — A preference MUST be a file under the state directory,
  MUST NOT be written into a skill, and MUST be undone by deleting that file.
  The command that reverts a learned phrasing MUST cover it.
- **OSKA-EVO-34** — Judgments read and preferences recorded MUST appear in the
  report of the run, under the level of the ladder they belong to, in
  accordance with `OSKA-EVO-21`.
- **OSKA-EVO-35** — The quality gate MUST run the evaluation set of the skill
  under review where one exists and where a ranking is available, and MUST
  refuse a proposal whose own cases fail. A skill that carries no evaluation set
  MUST NOT be refused for that reason.
- **OSKA-EVO-36** — Publishing a change request for review MUST go through a
  publisher selected by name. The default publisher MUST leave the change
  request under the change request root and write nothing else.
- **OSKA-EVO-37** — A publisher that writes into a version-controlled
  repository MUST write the package into a new branch of a repository its
  caller names, in a working tree separate from any checkout a person uses, as
  one commit whose author is the actor who approved the publication, and MUST
  record the repository, the branch and the commit on the change request. It
  MUST refuse a version the base of the branch already holds, and MUST NOT move
  the change request out of `open`.
- **OSKA-EVO-38** — A publisher MUST NOT push a branch unless its caller asks
  for the push explicitly, and MUST NOT force a push, a branch or a worktree in
  any case. The serving process MUST NOT merge a branch it published.
- **OSKA-EVO-39** — Opening a pull request MUST be done by a command the
  operator configures, run with its arguments as a vector and never through a
  shell, with the branch and the title substituted into those arguments. What
  the command answers MUST be recorded on the change request.
- **OSKA-EVO-40** — A publisher that fails during a transition MUST NOT undo
  the transition or the composed change request. The failure MUST be recorded
  in the transition note.

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
capabilities that were discussed. The reviewer's job becomes judgment about
whether this should be a standing capability, not archaeology about what
happened.

The lifecycle is four states because three of them answer different questions.
`submitted` says an author is done. `approved` says a reviewer agrees the
procedure should exist. `published` says it validated against the catalog as
it is now, which is a separate fact and can fail after approval — the catalog
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
directory, and compares the catalog, the configuration, the schemas and this
specification before and after; the job has to finish. A scan of the syntax tree
stands behind it as a cheap second line, failing on a call that names a state
beyond `draft` and on an import of the lifecycle, a connector or the policy
engine. The scan sees only what is written, and the state of a proposal can be
set without calling anything: the run catches that, and the scan does not.

### Why the serving process cannot write the catalog

Publishing used to be a file written under the running server by the running
server. Everything about it was defensible one step at a time: the transition
already required a named actor, the quality gate already refused an invalid
file, and the catalog already picked the new version up without a restart.
What it added up to was a process that answers people holding a write handle on
the document that says what it is allowed to do. Nothing had to go wrong for
that to be the wrong shape. A prompt that reaches the lifecycle, a bug in the
actor check, a second surface added later by someone who did not read this
file — each is a single point between "answering" and "granting itself a new
capability", and there is no review stage in between, because the reviewer was
whoever happened to be holding the console.

Moving the effect one step does not weaken the lifecycle; it gives the last
step somewhere to land that a person reads. `published` is still a decision by
a named actor against a validated file. What it produces is a directory: the
exact bytes that would be served, the record of who asked and what the gate
said, and a paragraph explaining it. Somebody opens that directory, sees a new
capability being requested, and copies it into a catalog root or does not.
That is the review that was missing, and it costs one command.

The alternative considered and rejected was to keep writing the file and guard
the write — a flag, an allowlist of callers, a check that the process is not
serving. Every one of those is the promise again, written more elaborately.
Removing the parameter is the whole argument: a function that is not given a
catalog root cannot write into one, whatever calls it and for whatever
reason. The test that proves it does not read the source, because reading the
source only says what is written today; it runs a turn, a draft and a whole
lifecycle with every write into the catalog raised as an error, and hashes
the tree before and after.

### Why a branch, and why never a push by default

A change request in a directory is reviewed by whoever thinks to look there.
The people who own a catalog kept in a repository already review changes as
branches, with the history, the diff and the tools they trust. Publishing a
request as a branch meets them there without giving the runtime any new power:
a branch is a proposal, and merging it is still a person's act.

The push is withheld by default because it is the step that leaves the
machine. A branch in a local repository can be read, amended or deleted by the
operator before anyone else sees it; a pushed branch is already in front of an
audience. The operator decides when that happens, and the runtime never forces
anything, so a branch a person has already touched is never overwritten.

### Why learned phrasings are files and not edits

A learned phrasing could be appended to the triggers of the skill it belongs to.
It would rank identically and cost one fewer file. It is refused for two
reasons. An edit cannot be undone by deleting anything: undoing it means knowing
which lines were authored and which were learned, which is exactly the
distinction the edit destroyed. And an authored skill is a document a person is
answerable for; a person who finds sentences in it that nobody wrote stops
trusting the rest. A separate file keeps the authored artifact authored, and
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
a measurement into a control loop, and control loops built on a metric optimize
the metric. Worse, the thing that moved is usually not the thing to revert: a
new procedure legitimately takes cases from an old one, and a changed trigger
legitimately changes a score. The report says precision fell, which cases were
missed, and what it is being compared against. A person decides whether the
catalog got worse or the labeled set got old.

## In the reference implementation

- `src/oska/learning/` — `proposals.py` (store and drafting), `quality.py`
  (lints and report), `lifecycle.py` (transitions and publication),
  `changes.py` (the change request, its store, and the only function that
  writes into a catalog root).
- `configs/runtime.json` — the change request root under `changes`.
- `src/oska/learning/publishers.py` — `ChangePublisher` with the `directory`
  and `git` publishers, and `PullRequestOpener` with `CommandOpener`, which
  runs the operator's command as an argument vector.
- `src/oska/cli/commands_change.py` — `change list`, `change show`,
  `change apply --into ROOT --by NAME`, `change reject`, and
  `change publish ID --repo PATH [--path DIR] [--base REF] [--push]
  [--open-pr COMMAND]`; applying names the root and then says that serving it
  means building a revision.
- `tests/unit/test_publishers.py` — a branch in a temporary repository with
  the package and the actor as author, no push by default, a push to a bare
  repository on disk when asked, and the command run without a shell.
- `tests/unit/test_changes.py` — the store, and the invariant as an executable
  document: a turn, a draft and a full lifecycle to `published` run with every
  write into the example catalog raised as an error, and the tree hashed
  before and after.
- `src/oska/contracts/proposal.py` — the proposal, its states and its
  history.
- `schemas/proposal.schema.json` — the normative proposal contract.
- `configs/runtime.json` — the publish root under `learning`.
- `src/oska/cli/commands_propose.py` and the `/proposals` routes of
  `src/oska/api/routes.py` — the two surfaces that draft a candidate and
  move it, both calling the same `transition`, the command line with the
  operator at the terminal as its actor and the HTTP routes with the caller's
  principal, or, with no identity where none is required, the name the call
  stated; both report the change request the move composed. `GET /changes`
  and `GET /changes/{id}` read those requests; reading proposals and change
  requests and moving a proposal need the reviewer role
  (`tests/unit/test_channel_identity.py`); there is no route that applies one,
  and `tests/unit/test_api.py` lists what the application declares to prove it.
- `examples/ops/scenarios/15_learning_lifecycle.json` — the lifecycle as an
  executable document: a session, a drafted candidate, a move without an actor
  that is refused, and a publication the catalog picks up.
- `src/oska/improvement/feedback.py` — the judgment on an answer, one
  append-only file per session under the state directory, and the rule that the
  last judgment of a turn is the one that counts.
- `src/oska/improvement/preferences.py` — the classification done at job
  time, the comment asking for an action that is discarded rather than stored,
  and the preference file that `improve revert` deletes.
- `src/oska/engine/reply.py` — the one place a preference is read, which is
  the only place it may travel to.
- `src/oska/improvement/` — `signals.py` (the run, read back), `learned.py`
  (phrasings kept beside a procedure, and reverted), `tuning.py` (bounded
  threshold search and the overrides file), `evaluation.py` (the labeled set
  and its report), `gaps.py` (clusters and ontology suggestions), `job.py` and
  `report.py` (the four steps and what they wrote).
- `src/oska/cli/commands_improve.py` — `improve run`, `improve show`,
  `improve feedback`, `improve revert` and `improve evaluate`; a run whose
  precision is below the previous report leaves a non-zero status and changes
  nothing, and `improve revert` names either a procedure whose phrasings are
  deleted or a session whose preferences are.
- `configs/runtime.json` — the improvement root, the report path, the caps and
  the bounds under `improvement`.
- `examples/ops/evaluation/routing.json` — the labeled routing set, in English
  and Italian, with two cases that expect a tie.
- `examples/ops/evaluation/redactions.json` — the patterns applied to a learned
  phrasing before it is written.
- `src/oska/index/base.py` — where the learned files are read back and
  fingerprinted, and where a ranking is labeled declared, derived or learned;
  `src/oska/engine/factory.py` wires them into the two layers that rank the
  catalog.
- `tests/unit/test_improvement.py` — the ladder as an executable document: the
  run against a store that refuses anything but a draft, which is what proves
  the ceiling, the scan of the syntax tree behind it, and the revert that gives
  the previous ranking back.
