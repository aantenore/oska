# 10 Cross-cutting concerns

## Purpose

Four concerns belong to no layer and constrain all of them: configuration,
observability, identity and evaluation. They are specified here so that each
layer document can stay about its own job, and so that the dependency rule
itself has somewhere to be stated as a testable requirement.

## Modules

### Configuration

Responsibility: hold every value that tunes behaviour, outside the code, in one
place per concern, replaceable without a rebuild.

```text
Settings:
  catalogue_roots: [path]
  runtime_config: path
  model_config: path
  connectors_config: path
  state_dir: path
  provider: none | scripted | remote
  # every field overridable by an environment variable with a common prefix
```

The runtime configuration carries the numbers the layers read: discovery
minimum score, tie margin, result limit and excluded types; router token lists
for approval, rejection and cancellation, the fast-path switch, its threshold
and its utterances; ledger and audit locations; the default reply language; the
publication root. None of these appears as a literal inside logic.

Connector bindings and model provider settings are separate documents, because
they change for different reasons and often under different authority.

### Observability

Responsibility: make a session answerable after the fact, from artefacts alone.

Three artefacts, each addressing a different question:

- **Audit events** — what the system did, step by step, typed, with a session
  and turn identifier on every entry. Sinks are pluggable: memory for tests, an
  append-only file for a deployment, fan-out to several.
- **Receipts** — what changed in the world, with the payload sent, the response
  received, the verification status and the evidence behind it. Durable across
  restarts.
- **Scenario replays** — what the system would do, deterministically, for a
  recorded conversation against a scripted provider.

Together they answer: what happened, what changed, and would it happen again.

A fourth artefact answers none of those and is kept apart for that reason.
**Diagnostic traces** record how long each stretch of a turn took and what it
was nested inside: the turn, the routing, the discovery, the planning, the
execution and the reply. They are for whoever has to explain a slow turn, and
they are optional, off unless a deployment turns them on, and safe to throw
away. Nothing in the runtime reads a trace back and no decision is taken from
one. A question that can only be answered from a trace is a missing audit
event, not a reason to leave tracing on.

### Cost

Responsibility: bound what one turn may spend.

A turn has a budget of model calls, declared in the runtime configuration and
counted in one place, so that every call the runtime makes — classification,
planning, tie breaking and phrasing — is counted the same way. Past the budget
the model is not called again. The turn does not fail: it records that it
stopped, and answers deterministically from what it already has.

```text
TurnConfig:
  max_model_calls: integer

ModelBudget:
  start_turn()
  take() -> boolean      # false once the turn has spent its budget
  limit, spent, refused
```

Work that genuinely needs more than one turn's budget belongs where it can be
paid for once and read many times: at publication, or in a scheduled job.

### Identity

Responsibility: carry the actor.

OSKA assumes an authenticated channel. It does not authenticate anyone. What it
requires is that the two human acts — approving a write and transitioning a
proposal — carry an actor identifier that is recorded and never defaulted. A
deployment that puts an unauthenticated channel in front of the engine has
disabled the guarantees of L4 and L7 regardless of what the engine does, and the
documentation of a conformant system must say so plainly.

```text
Approval and transition records:
  by: string        # actor identifier supplied by the channel
  at: timestamp
```

The actor is recorded, not verified. Verification belongs to the channel.

### Evaluation

Responsibility: make the specification executable.

A scenario is a recorded conversation plus the expectations that must hold
after every turn: the route kind, fragments the reply must contain, the exact
outcome kinds in order, what the session is waiting for, how many receipts
exist and the verification status of the last one. Model calls are answered by
a scripted provider whose steps assert what the prompt that reached them
contained, so a change in the order of the calls fails loudly rather than
passing quietly. Connector behaviour can be injected per turn, so failures are
reproducible.

Scenarios are the primary conformance evidence. Unit tests prove module
contracts; scenarios prove the architecture.

## Invariants

- No threshold, path, limit, token list, model name or port appears as a
  literal in logic.
- A layer imports only layers below it and contracts.
- Contracts are declared once and owned by no layer.
- Every audit event carries a session identifier and a turn identifier.
- Receipts and audit records are append-only with respect to history: an update
  fills in the outcome of an existing record, it does not erase one.
- The conformance suite runs offline, with no credentials and no network.
- Any recorded actor is recorded verbatim; the system never substitutes a
  default actor.
- Traces are diagnostics. The account of what the system did is the audit
  trail, and no behaviour depends on a trace being recorded.
- Tracing is off unless configuration turns it on, and the conformance suite
  runs with it off.
- A turn spends no more model calls than its declared budget, and a turn that
  reaches it still answers.

## Requirements

- **OSKA-XC-1** — A module MUST NOT import from a layer above it; the layering
  MUST be checkable statically.
- **OSKA-XC-2** — Types shared by more than one module MUST be declared once, in
  the contracts, and MUST NOT be redeclared by a consuming module.
- **OSKA-XC-3** — Every tunable value MUST be read from external configuration;
  a threshold, limit, path, token list or model name MUST NOT be a literal in
  logic.
- **OSKA-XC-4** — Configuration MUST be overridable by environment without
  editing a file, and the effective settings MUST be inspectable at run time.
- **OSKA-XC-5** — Every step of a turn MUST emit a typed audit event carrying
  the session identifier, the turn identifier and a timestamp.
- **OSKA-XC-6** — The audit sink MUST be replaceable, and at least one durable
  append-only sink MUST be available.
- **OSKA-XC-7** — Receipts MUST survive a restart of the runtime and MUST remain
  retrievable by session.
- **OSKA-XC-8** — An approval and a proposal transition MUST each record an
  actor identifier supplied by the channel; the system MUST NOT supply a
  default actor.
- **OSKA-XC-9** — The conformance suite MUST run offline, without credentials
  and without network access, against a scripted provider.
- **OSKA-XC-10** — A scenario run MUST fail when the model calls occur in a
  different order or with different prompt content than the script records.
- **OSKA-XC-11** — Diagnostic traces MUST be distinct from the audit record. A
  trace MUST NOT be required to answer what the system did, MUST NOT carry a
  step that the audit trail does not also carry, and no decision MUST be taken
  from one.
- **OSKA-XC-12** — Tracing MUST be off unless configuration turns it on, its
  destination MUST be configurable, and the conformance suite MUST run without
  writing traces.
- **OSKA-XC-13** — A turn MUST NOT make more model calls than the configured
  per-turn budget. Once the budget is spent the runtime MUST NOT call the model
  again in that turn, MUST emit a typed audit event recording what was spent
  and refused, and MUST still answer from what the turn already holds.
- **OSKA-XC-14** — The per-turn budget MUST be counted in one place, so that
  every model call the runtime makes is counted whichever part of the turn
  makes it.

## Rationale

Configuration as an external document is the least interesting of the four and
the one that decides whether the other three are true. A threshold compiled into
the code is a behaviour nobody can inspect and no reviewer can diff. Discovery's
minimum score is the difference between answering a request and declining it;
the fast-path threshold decides whether a model is consulted at all. Those are
policy decisions in the ordinary sense, and they belong in a file that an
operator can read, change and put under review, next to the connector bindings
and the policy documents.

Observability here is deliberately three artefacts rather than one log. A log
answers "what happened" and is terrible at the other two questions. Receipts
exist because "what changed in the world" must be answerable without replaying
and re-interpreting a stream of events, and because the retry decision depends
on it — the ledger is the same object. Scenarios exist because neither of the
other two tells you whether the behaviour is intentional. A recorded
conversation that fails when the sequence of model calls changes is the only
artefact that makes the turn algorithm a thing you can refactor.

On identity, the honest position is the narrow one. Building authentication
into an architecture standard would either be ignored or would force a
deployment model nobody wants. What the standard can require is that the two
consequential acts carry an actor, that the actor comes from outside, and that
no default is ever substituted — because a default actor turns an audit trail
into decoration. The deployment supplies the proxy; the standard supplies the
requirement that there must be one, stated where operators will read it.

Traces are separated from the audit trail for the same reason receipts are
separated from events: a record that answers everything answers nothing
particularly well. The pressure on a trace is to be cheap and detailed, the
pressure on an audit event is to be typed, durable and complete; letting the
two merge means either an audit trail nobody keeps or a trace nobody can afford
to write. Keeping tracing off by default is part of that separation. If a
system needed its traces on to be accountable, the traces would have become the
audit trail under another name.

The per-turn budget is a cost bound that behaves like a correctness bound. A
turn that may call the model as often as it likes has no worst case, and the
worst case is what a deployment has to pay for and what a user waits through.
Counting in one place is what makes the bound real: a count kept by each caller
is a count that a new caller silently escapes. The turn answering anyway,
deterministically, is the other half — a bound that turns a turn into an error
would simply be removed the first time it fired.

## In the reference implementation

- `src/opsdeck/config.py`, `configs/runtime.json`, `configs/model.json` —
  settings and the tunable values.
- `src/opsdeck/cli/main.py` — `opsdeck config`, which prints the settings in
  force after the environment has been applied, with secrets masked.
- `src/opsdeck/audit/sink.py` — memory, file and fan-out sinks.
- `src/opsdeck/audit/trace.py` — the diagnostic span sinks: none, memory and
  file, with the null sink in force whenever tracing is off.
- `src/opsdeck/engine/engine.py` — the per-turn model budget and the provider
  wrapper that counts every call through it.
- `src/opsdeck/governance/ledger.py` — durable receipts.
- `src/opsdeck/contracts/` — the shared types, owned by no layer.
- `tests/scenarios/test_scenarios.py` — the scenario runner, including the check
  that the scripted model calls were consumed in the recorded order.
