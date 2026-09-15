# Architecture

How the runtime is put together, package by package, and what one turn actually
does. The layer names come from [`spec/02-layers.md`](../spec/02-layers.md);
this document is about the code under `src/opsdeck/`.

## The runtime, layer by layer

### Contracts

`opsdeck.contracts` holds the typed models every layer shares and no layer
owns: `skill` (frontmatter, operations, entities, sections), `policy`,
`plan`, `receipt`, `proposal`, `events`, `operation`, and `common`, which
carries the turn-level types — `Message`, `RouteDecision`, `Outcome`,
`PendingApproval`, `PendingGoal`, `TurnResult`, `Hydrated`, `ConnectorResult`.
Nothing in `contracts` imports another opsdeck package.

### Configuration

`opsdeck.config` reads three things: environment variables prefixed
`OPSDECK_` for paths and provider selection, `configs/runtime.json` for
thresholds and routing words, `configs/model.json` for provider access, with
the environment overriding it. No threshold is written in code. See
[configuration.md](configuration.md).

`opsdeck.services` assembles the runtime once — engine, proposal store,
settings, publication root — and both channels work against that assembly, so a
turn over HTTP and a turn in a terminal cannot drift apart.

### L1 Integration

`opsdeck.connectors` runs a declared operation against a system. Three
adapters: `MockConnector` reads and writes an in-memory copy of a JSON fixture;
`HttpConnector` fills a path template and sends the fields the binding lists as
query or body; `ShellConnector` renders a declared argument vector, one payload
value per argument, and never builds a command string or hands anything to a
shell. `ConnectorRegistry.from_config` binds each integration id to one
connector and expands `${NAME}` in addresses and headers from the environment.

A connector receives an operation name and a payload. It never receives free
text, and it never sees the conversation.

Availability is part of what it answers, and `connectors/base.py` is where that
lives. Every result says what kind of failure it was: `transport` for a call
that did not reach the system or that the system did not complete, `refused`
for a system that answered no, `rejected` for a request that was never well
formed. Only `transport` is retryable, and only it counts toward the breaker,
which sits in `BaseConnector` around every call. After
`resilience.failures_before_open` consecutive transport failures the connector
stops being called for `resilience.recovery_seconds`, then lets one attempt
through, which either closes it or opens it again. An adapter implements
`_perform` and says how a call failed; the timeout, the breaker and the health
reporting belong to the base class, so a binding cannot forget them and a call
the breaker refuses never reaches the adapter at all.

### L2 Ontology

`opsdeck.ontology` builds a graph from the catalog: entity nodes with their
fields, identifier, state field and states; relation edges; operation nodes
bound to the entity they act on; procedure edges for the runbooks that reach
them. `queries` answers the structural questions — `operations_on`,
`writers_of`, `procedures_for`, `related`, `vocabulary`, `path` — and `export`
renders the graph as JSON or mermaid. `OntologyCache` rebuilds it when the
catalog fingerprint changes.

An entity may also declare a lifecycle: the state it starts in, the states it
never leaves, the read its current state is taken from, and the legal moves
between states with the operation that causes each. `lifecycle_of`,
`legal_moves` and `required_move` answer from the graph; `export` renders one
mermaid state diagram per lifecycle, after the entity diagram, because a
lifecycle is a machine of its own and putting states in the entity graph
confuses two pictures.

The graph is derived. Nothing is declared twice: the entities come from entity
skills and the bindings from the `entity` field of each declared operation.

### L3 Skill

`opsdeck.catalog` is the catalog. `loader` parses a skill file into typed
frontmatter plus H2 sections and fingerprints its text. `registry` discovers
every `**/v*/skill.md` under the configured roots, keeps the effective version
of each id — highest published, or highest of all when none is published — and
reloads when the set of files, their sizes or their modification times change.
`closure` hydrates a procedure into the entities, integrations and member
runbooks it reaches. `validator` checks each skill against the JSON schema and
against the rest of the catalog; the rules and their codes are in
[skill-contract.md](skill-contract.md). `resolver` derives every sentence the
runtime says about itself: the declared scope, the phrase for one procedure,
the actions available on an entity. Where the entity declares a lifecycle,
`suggestions_for_entity` answers from the state the object is in rather than
from the list of procedures that touch it, and leaves out a move the author
marked as not offered. An offer travels as a `SuggestedAction`: a key that
names the action and the wording its author declared, which is what lets the
reply writer return the wording in the language of the conversation without
anything between the two being invented.

`enrichment` is one of three parts of this layer that never run during a turn.
It paraphrases the declared triggers of a procedure into further languages and
writes them to a file inside the skill's version directory, stamped with a hash
of the triggers they came from. The index reads that file; a hash that no
longer matches means the file is ignored rather than repaired. See
[multilingual.md](multilingual.md).

`evaluation` is the second. A skill may carry an `evaluation.json` beside it
holding the requests that must reach it and the requests that must not, and
`evaluate_catalog` runs every such set through the same ranking the runtime
uses, handed in as a `Ranking` protocol rather than imported, so the catalog
layer does not reach up into the layer that ranks it. `cases_from_neighbors` drafts the
half of a set that is hard to write, from the declared triggers of the skills
that rank closest; it prints and writes nothing.

`revision` is the third, and it is what a deployment of more than one process
serves from. `build_revision` reads the skill roots, validates them, refuses a
catalog carrying an error, copies what held up into a directory that is never
written again, and writes a manifest naming every skill with the hash of its
content. The identifier is derived from the tenant and that map of hashes
alone — not from when or where the build ran — so the same inputs always give
the same identifier and two replicas building the same catalog agree on what to
call it. `RevisionStore` holds the built revisions, one directory per tenant,
and records in a `CURRENT` file beside them which one this deployment serves.

`registry` has two shapes because of that. `SkillRegistry` over a directory
fingerprints the files it discovers and reloads when they move; over a revision
it reads once, because a revision cannot change. `ServedCatalog` is the second
with the revision left open: it holds one registry per revision, and answers
through whichever revision the turn under way is pinned to. The pin is a
context variable, because one engine serves overlapping turns and the revision
in force belongs to the turn rather than to the process. Which revision a turn
that pins nothing reads is re-resolved rather than cached — the configured
revision, failing that the one the store records, failing that the newest — so
a deployment moved to another release is followed without a restart.

### L4 Governance

`opsdeck.governance` is the write path. `capabilities` answers what the catalog
declares an operation to be and whether a procedure may reach it. `policy_engine`
applies the policy file a procedure carries, defaulting to deny. `approval`
freezes a pending write on a hash of its exact payload. `verification` runs the
read-back the integration declares and compares the values it expects. `ledger`
keeps every write attempt in SQLite, keyed on the idempotency fields, written
before the call goes out. `guardrails` reads the catalog of promises this
installation declares and builds, for each one kept by code, the check that
keeps it; nothing in that module carries a pattern, a threshold or a word list
of its own. `opsdeck.audit` appends one typed event per step to a JSONL file,
and `audit.trace` opens diagnostic spans that are off by default and are never
read back by anything. See [governance.md](governance.md).

Two more modules sit here. `identity` holds the `Principal` a channel
authenticated and resolves the payload fields a procedure declares as taken
from it; it authenticates nothing, because what the channel reported is the
only thing it has. `retention` is the only place in the system that takes
anything out of the record: it removes the receipts, the audit entries and the
stored state of a session together, and writes one event saying that it did.

### L5 Cognition

`opsdeck.index` ranks the catalog against a request. `KeywordIndex` scores
token overlap; `EmbeddingIndex` embeds trigger text and descriptions through the
provider; `Discovery` chooses the top candidate when it clears `min_score` and
reports a tie when the second is within `tie_margin`. Only published skills with
triggers, of a type not excluded, are candidates — so composites are ranked out
by configuration and entities and integrations never compete with procedures.
A skill is indexed under several texts — its description, each of its triggers,
the phrases derived beside it when enrichment is on, and the phrasings the
improvement job has learned for it — and scores the best of them, so more
phrasings can raise a score and never dilute one. Both extra files are read and
never written here: a turn ranks the catalog from what is already on disk. The
cap on how many texts one skill contributes never drops a declared one, and
`matched_on` says which text won.

`opsdeck.router` decides what a turn is, in three layers: `RuleRouter` matches
the whole message against the configured approve, reject and cancel words, and
only while the session is waiting for something; `FastPath` matches the message
against the triggers authors wrote down, above a high threshold, and only when
nothing is pending; `ModelRouter` asks the model to classify against a prompt
that names the declared scope and the procedures on offer.

`opsdeck.planner` turns one procedure into a typed plan. `Planner.plan` builds
the prompt from the hydrated procedure — its sections, its entities, its
ontology facts, its declared operations with input schemas — asks for a plan,
validates it against those declarations, and on failure asks once more with the
issues appended. Among those declarations are the fields a procedure reserves
to a person: a payload that carries a value for one of them which the person
did not give is an issue, whatever else about the plan holds up, and the field
belongs in `missing` with the question to ask instead. A plan that still does
not hold up raises `InvalidPlanError`, which the engine records as an outcome.
`Planner.pick` resolves a discovery tie.
`Planner.write_reply` phrases a reply from the outcomes of the turn.
`provider.py` is the interface: `complete_json` and `embed`.
`openai_compatible.py` speaks the chat-completions protocol; `scripted.py`
replays recorded answers offline.

### L6 Interaction

`opsdeck.engine` runs the turn. `engine.py` is the algorithm below.
`execution.py` runs the operations of a plan under the declarations that govern
them, and it is where a field declared as a principal input is filled: after
the model has finished proposing operations and before anything judges or
performs one. `goals.py` works a goal — one procedure, or the ordered
procedures of a composite — forward until something stops it. `reply.py` builds
the reply text, appending the deterministic line of every receipt and ledger
hit to whatever a model wrote, so a write is always accounted for by its
receipt. `session_store.py` keeps session state in SQLite. `factory.py` wires
everything from settings, including which catalog is in force: a `ServedCatalog`
over the revision store when this deployment has built one, the configured
skill roots read directly when it has not. The ledger is wrapped either way, so
every receipt opened in a turn names the revision the turn was decided under.

Three wrappers sit around that loop, all built in `factory.py` from the runtime
configuration. `BudgetedProvider` counts the completions one turn makes and
refuses the one past the limit; embeddings are not counted, because they are
how the index reads the catalog and cost the same whatever the turn decides.
`GuardrailChain` runs the checks of the input point over what arrived and the
checks of the output point over the reply. The trace sink times the stretches
of the turn, and a traced executor and a guided planner are the two places
spans are opened around work the engine did not write itself.

`opsdeck.api` serves the runtime over HTTP; `opsdeck.cli` serves it in a
terminal. Both call the same engine methods.

### L7 Evolution

`opsdeck.learning` drafts a candidate procedure from what a session did, runs
the quality checks, and moves a proposal through its states with a named actor.
Publishing one composes a change request in `changes.py` — the package, its
origin and the quality report, under a root a person reads — and installing
that package is a separate act by a person. See [learning.md](learning.md).

`opsdeck.improvement` is the scheduled half of the same layer: it reads the
audit trail and the ledger back as signals, reads the judgments people
recorded against turns, keeps the phrasings they actually used in files it can
delete again, proposes threshold changes inside declared bounds, routes a
labeled set and reports how it went, and turns a cluster of unanswered
requests into a proposal in `draft`. `preferences.py` is the narrowest thing it
writes: a note about how one session likes its answers written, which reaches
the reply writer and nothing else. It imports neither the lifecycle that moves
a proposal nor anything that writes a policy or a connector binding. See
[improvement.md](improvement.md).

`opsdeck.conformance` replays scenario files against a fresh runtime and reports
the level reached. See [conformance.md](conformance.md).

### Beside the layers

`opsdeck.observability` is in no layer, because it decides nothing. It reads
back two records written for other reasons: the audit trail, which is typed,
durable and required, and the traces, which are optional and off by default.
From the trail it computes how often a deterministic template spoke because no
model could, how often a read-back did not confirm a write, how often a
guardrail refused, and how often a request matched nothing in the catalog; from
the traces, the median and the tail of how long a turn takes. `Measure` is the
type that makes this honest — a number the deployment did not record carries
the reason it is missing rather than a zero — and `report.py` prints the same
thing as prose or as JSON, naming what is absent either way.

## The turn algorithm

`Engine.turn(session_id, text, principal=...)` loads the session, binds the
identity, opens the catalog on one revision, resets the model budget, opens the
`turn` span, runs the turn, and saves the state in a `finally` block, so a turn
that fails halfway still leaves what it decided on disk.

Two things happen before step 1 and belong to the turn rather than to the
engine, because one engine serves overlapping turns.

**The identity is bound.** `principal` is what the channel authenticated. A
session opened without one stays open to whoever arrives first; a session that
holds one refuses a turn arriving under a different subject or under none,
raising `PrincipalMismatch` before anything of the turn is run or recorded,
because a session is one conversation with one person and a write bound to an
identity halfway through would be bound to whichever one happened to be on that
turn. Nothing here verifies anything: the runtime trusts the channel, and a
principal the channel marked as not authenticated fills no declared field.

**The revision is pinned.** A session that has never been served takes what the
deployment serves now. A pinned session keeps what it was served by; a
deployment with `catalog.pin_sessions` off still holds a session to its
revision while an approval waits under it, because the sentence that was
approved was composed from that revision. Only a turn that starts a new task
adopts the newest, and the switch is recorded as a `revision` event naming what
it moved from, what to, and why. When the catalog is read from a directory
rather than a revision store, pinning is a no-op and the caller need not know
which deployment it is talking to.

1. **Open.** Increment the turn count, settle the language to the configured
   default if the session has none, run the input guardrails over the text,
   emit `turn` with the text as those guardrails left it, append it to the
   history. A guardrail that refused ends the turn here: the reply is what the
   guardrail itself says, a `guardrail` event names which one acted, the
   outcome is `denied`, and nothing the session was already waiting for is
   touched, because nothing was decided.
2. **Route.** Ask the router with a context that says whether a goal is under
   way and whether a write is waiting. Adopt the language the decision reports.
   Emit `route` with the kind, the procedure named, the confidence and which
   layer answered.
3. **Dispatch** on the kind. A call the budget refuses raises out of the
   dispatch: the pending goal is dropped, a `cancelled` outcome records that
   the turn reached the calls it is allowed to make, and the turn still
   answers.
4. **Reply.** Build the text, run the output guardrails over it, append it to
   the history, emit `budget` if anything was refused, emit `reply`, return the
   reply, the outcomes, the pending item and the events of the turn.

The guardrail boundaries are the two edges of the turn and nothing in between:
what arrives before anything is decided, and what leaves before anyone reads
it. A promise kept by a check is never also put in a prompt, because handing
the model a rule the code already enforces invites it to argue with the code;
the instruction guardrails a hydrated procedure declares are what reaches the
planning and reply prompts instead.

Spans nest inside the `turn` span: `route`, `plan` once per procedure planned,
`execute` once per stretch of a plan and once per approved write, and `reply`.
Each records how long it stayed open and what it was nested in. When tracing is
on, the turn also emits a `trace` event naming where the spans are being
written, so the audit trail says that a diagnostic record exists without
becoming one.

### Dispatch

- **greeting**, **off_scope** — answer from the resolver and stop.
- **cancel_task** — emit `approval_decided` with `cancelled` if a write was
  waiting, record a `cancelled` outcome, clear the pending approval and the
  pending goal.
- **reject** — with nothing waiting, off scope. Otherwise emit
  `approval_decided` with `rejected`, record a `rejected` outcome, clear both.
- **approve** — with nothing waiting, off scope. Check the payload hash; a
  mismatch cancels the approval and reports it. Increment the approvals given
  and emit `approval_decided`. If a second approval is still required, hold.
  Otherwise re-read what the plan read before the write, re-evaluate the policy
  against the state as it is now, and — if it still allows, and the move is
  still a legal one for the object — record the intent, execute, verify, then
  continue the rest of the plan and advance the goal.
- **provide_input**, **continue_task** — with a goal under way and no approval
  pending, store the answer against the first missing field and advance the
  goal. Otherwise treated as a new request.
- anything else — **new task**. A write waiting for approval is cancelled
  first: a new request replaces a goal, it never merges with it. If the router
  did not name a procedure, discovery ranks the catalog and the planner breaks a
  tie. A procedure that is not a runbook or a composite is off scope. A new
  pending goal is set — the runbooks of a composite in order, or the single
  runbook — and the goal is advanced.

### Advancing a goal

For each step: hydrate the procedure and emit `hydrate`; with no provider,
answer that planning is unavailable; otherwise plan and emit `plan`. A plan with
missing inputs stops the turn with a question. Otherwise the executor runs the
operations in order. Reads run immediately and their records are collected for
the policy and for the suggestions. A write stops the plan: see
[governance.md](governance.md) for the checks it passes through.

An operation whose connector reports that the call never arrived is neither a
result nor a refusal, and is recorded as an `unavailable` outcome carrying the
binding, the operation and when it may be tried again. The plan carries on past
it: a binding that is down takes its own operations with it and nothing else.

One of those checks is worth naming here, because of where it sits. When the
write acts on an entity that declares a lifecycle, the engine reads the current
state — from what the plan already read, or through the entity's declared read
when it did not — and looks for a move from that state caused by this
operation. If there is none, the plan ends with an `invalid_plan` outcome
naming the state the object is in and the moves that are legal from it, and no
approval is requested at all. Asking a person to approve something the object
cannot do is not a governance step, it is a question with one acceptable
answer. The same check runs again when the approval comes back, because the
state may have moved while the question was waiting. When a plan
completes, the goal moves to the next procedure; when the list is exhausted the
goal is cleared and the reply may suggest what can be done next with what was
read.

### Model calls, in order, by route kind

With no provider configured, none of these happen: routing falls back to rules
and triggers, discovery ranks by keyword overlap, and a turn that needs a plan
answers that planning is unavailable.

| Route kind | Model calls, in order |
| --- | --- |
| refused by an input guardrail | none: the turn ends before it is classified |
| greeting (fast path) | none |
| greeting, off_scope (model) | classify |
| new_task | classify, then pick if discovery ties, then plan (plus one repair if the plan does not hold up), then write reply if the turn ends in outcomes |
| provide_input, continue_task | classify, then plan per procedure advanced, then write reply if the turn ends in outcomes |
| approve, last one required | classify or none (`yes` is matched by rule), then plan for any further procedure of a composite, then write reply |
| approve, more required | classify or none, then nothing: the approval text is deterministic |
| reject, cancel_task | classify or none, then write reply |

Two rules hold across the table. Classification is skipped whenever the rule
layer or the fast path answers, which is what makes a scripted scenario able to
assert how many classifications actually happened. And the reply is written by a
model only when its kind is `outcomes`; the scope sentence, the question for a
missing input and the approval request with its exact payload are built from
declarations and never pass through a model, because that text has to match the
payload word for word. Embedding calls are separate: discovery and the fast path
each build an index when the catalog fingerprint changes, and embed the query on
every use, and they are outside the budget for the same reason.

The budget is the ceiling over the whole table. `turn.max_model_calls` is four
by default: what an ordinary new task spends is classify, plan and a reply
written from the outcomes, which leaves one for a tie break or a plan repair. A
turn that needs more than its budget does not get more; it says so. That is also the rule any
new feature is measured against: work that would add a call to a turn belongs
at publish time or in a scheduled job instead, which is where enrichment and
the improvement job are.

## The dependency rule

A layer depends only on the layers below it and on the contracts. Three things
in the code carry it.

**Cognition does not execute.** `opsdeck.planner` and `opsdeck.router` import
`catalog`, `ontology` and `contracts`. Neither imports `connectors` or
`governance`. The planner emits a `Plan`; `engine.execution` is what turns a
planned operation into a connector call, and only after the governance checks.

**Governance does not think.** `opsdeck.governance` imports `catalog` and
`contracts`, and nothing else of opsdeck. The verifier needs a connector to
read back with, and declares the one method it uses as a local `Protocol`
rather than importing an adapter. It never sees the planner or the provider. A
policy decision is a function of the declarations, the payload, the entities
that were read and the ledger.

**Evolution proposes and does not install.** `opsdeck.learning` parses,
validates and renders skill files, and publication composes a change request: a
package written under the change request root for a person to read.
`transition` takes no catalog root and cannot be given one. Copying that
package into a catalog root is `ChangeRequestStore.apply`, which takes the root
from whoever calls it and is called from a terminal and from nowhere in the
engine, the HTTP application or a job. So the process that answers people can
propose a change to the catalog it answers from and can never make one.
`opsdeck.improvement` sits under a tighter rule again: it reads the ledger and
the audit trail, writes only files under its own roots and one report, and
imports neither the proposal lifecycle nor anything that writes a policy or a
connector binding. A test walks the syntax tree of every module in the package
to keep it that way.

The engine is the only place the layers meet, which is why it is the only place
the order of the steps is written down.
