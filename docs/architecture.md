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

### L2 Ontology

`opsdeck.ontology` builds a graph from the catalog: entity nodes with their
fields, identifier, state field and states; relation edges; operation nodes
bound to the entity they act on; procedure edges for the runbooks that reach
them. `queries` answers the structural questions — `operations_on`,
`writers_of`, `procedures_for`, `related`, `vocabulary`, `path` — and `export`
renders the graph as JSON or mermaid. `OntologyCache` rebuilds it when the
catalog fingerprint changes.

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
the actions available on an entity.

### L4 Governance

`opsdeck.governance` is the write path. `capabilities` answers what the catalog
declares an operation to be and whether a procedure may reach it. `policy_engine`
applies the policy file a procedure carries, defaulting to deny. `approval`
freezes a pending write on a hash of its exact payload. `verification` runs the
read-back the integration declares and compares the values it expects. `ledger`
keeps every write attempt in SQLite, keyed on the idempotency fields, written
before the call goes out. `opsdeck.audit` appends one typed event per step to a
JSONL file. See [governance.md](governance.md).

### L5 Cognition

`opsdeck.index` ranks the catalog against a request. `KeywordIndex` scores
token overlap; `EmbeddingIndex` embeds trigger text and descriptions through the
provider; `Discovery` chooses the top candidate when it clears `min_score` and
reports a tie when the second is within `tie_margin`. Only published skills with
triggers, of a type not excluded, are candidates — so composites are ranked out
by configuration and entities and integrations never compete with procedures.

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
issues appended. A plan that still does not hold up raises `InvalidPlanError`,
which the engine records as an outcome. `Planner.pick` resolves a discovery tie.
`Planner.write_reply` phrases a reply from the outcomes of the turn.
`provider.py` is the interface: `complete_json` and `embed`.
`openai_compatible.py` speaks the chat-completions protocol; `scripted.py`
replays recorded answers offline.

### L6 Interaction

`opsdeck.engine` runs the turn. `engine.py` is the algorithm below.
`execution.py` runs the operations of a plan under the declarations that govern
them. `goals.py` works a goal — one procedure, or the ordered procedures of a
composite — forward until something stops it. `reply.py` builds the reply text,
appending the deterministic line of every receipt and ledger hit to whatever a
model wrote, so a write is always accounted for by its receipt.
`session_store.py` keeps session state in SQLite. `factory.py` wires everything
from settings.

`opsdeck.api` serves the runtime over HTTP; `opsdeck.cli` serves it in a
terminal. Both call the same engine methods.

### L7 Evolution

`opsdeck.learning` drafts a candidate procedure from what a session did, runs
the quality checks, moves a proposal through its states with a named actor, and
publishes an approved one into a catalog root the registry reads. See
[learning.md](learning.md).

`opsdeck.conformance` replays scenario files against a fresh runtime and reports
the level reached. See [conformance.md](conformance.md).

## The turn algorithm

`Engine.turn(session_id, text)` loads the session, runs the turn, and saves the
state in a `finally` block, so a turn that fails halfway still leaves what it
decided on disk.

1. **Open.** Increment the turn count, settle the language to the configured
   default if the session has none, emit `turn`, append the user message to the
   history.
2. **Route.** Ask the router with a context that says whether a goal is under
   way and whether a write is waiting. Adopt the language the decision reports.
   Emit `route` with the kind, the procedure named, the confidence and which
   layer answered.
3. **Dispatch** on the kind.
4. **Reply.** Build the text, append it to the history, emit `reply`, return the
   reply, the outcomes, the pending item and the events of the turn.

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
  against the state as it is now, and — if it still allows — record the intent,
  execute, verify, then continue the rest of the plan and advance the goal.
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
[governance.md](governance.md) for the checks it passes through. When a plan
completes, the goal moves to the next procedure; when the list is exhausted the
goal is cleared and the reply may suggest what can be done next with what was
read.

### Model calls, in order, by route kind

With no provider configured, none of these happen: routing falls back to rules
and triggers, discovery ranks by keyword overlap, and a turn that needs a plan
answers that planning is unavailable.

| Route kind | Model calls, in order |
| --- | --- |
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
every use.

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

**Evolution writes into the catalog and nowhere else.** `opsdeck.learning`
parses, validates and renders skill files, and publishes into a directory that
is a catalog root. It does not touch the engine, the ledger or the connectors.
The runtime picks a published skill up the same way it picks up a hand-written
one: the registry notices the fingerprint changed.

The engine is the only place the layers meet, which is why it is the only place
the order of the steps is written down.
