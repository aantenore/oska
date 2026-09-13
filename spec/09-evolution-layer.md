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
