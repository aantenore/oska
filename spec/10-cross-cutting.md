# 10 Cross-cutting concerns

## Purpose

Four concerns belong to no layer and constrain all of them: configuration,
observability, identity and evaluation. They are specified here so that each
layer document can stay about its own job, and so that the dependency rule
itself has somewhere to be stated as a testable requirement.

## Modules

### Configuration

Responsibility: hold every value that tunes behavior, outside the code, in one
place per concern, replaceable without a rebuild.

```text
Settings:
  catalog_roots: [path]
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

Responsibility: make a session answerable after the fact, from artifacts alone.

Three artifacts, each addressing a different question:

- **Audit events** — what the system did, step by step, typed, with a session
  and turn identifier on every entry. Sinks are pluggable: memory for tests, an
  append-only file for a deployment, fan-out to several.
- **Receipts** — what changed in the world, with the payload sent, the response
  received, the verification status and the evidence behind it. Durable across
  restarts.
- **Scenario replays** — what the system would do, deterministically, for a
  recorded conversation against a scripted provider.

Together they answer: what happened, what changed, and would it happen again.

A fourth artifact answers none of those and is kept apart for that reason.
**Diagnostic traces** record how long each stretch of a turn took and what it
was nested inside: the turn, the routing, the discovery, the planning, the
execution and the reply. They are for whoever has to explain a slow turn, and
they are optional, off unless a deployment turns them on, and safe to throw
away. Nothing in the runtime reads a trace back and no decision is taken from
one. A question that can only be answered from a trace is a missing audit
event, not a reason to leave tracing on.

#### The service-level numbers

What a deployment is woken up for is not what it logs. A few numbers say almost
everything worth waking somebody for, over a stated window: how many turns there
were, how long a turn took at the median and at the 95th percentile, how often a
deterministic template spoke because the model did not, how often the read-back
after a write did not confirm it, how often a declared guardrail refused a turn,
and how often a request found no procedure at all.

All but the latencies come from the audit trail, which a conformant deployment
keeps anyway; the latencies come from the traces, which are optional and off by
default. So a deployment running with tracing off can answer some of these and
not others, and what it says about the ones it cannot answer is the part that
matters. A number nobody recorded is reported as not computed, naming why. A
rate printed as zero because nothing recorded it reads exactly like a rate that
is genuinely zero, and the two mean opposite things: one is a healthy deployment
and the other is a blind one. The same holds for a number that is computed and
covers less than its name suggests, which says what it leaves out.

Reading these numbers is reading. They are derived from artifacts the deployment
already keeps, they add nothing to the path of a turn, and computing them writes
to neither source; a number that would need a new recording in the turn is a
missing audit event, decided the way every other one is.

The last of them is not an error rate. A request that found no procedure is the
product signal that the catalog is missing something, and it is the one of
these numbers a healthy system can have too little of rather than too much.
`spec/12-conformance.md` sets them out one by one.

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
passing quietly. Connector behavior can be injected per turn, so failures are
reproducible.

Scenarios are the primary conformance evidence. Unit tests prove module
contracts; scenarios prove the architecture.

### Retention

Responsibility: keep the record for as long as it is needed and no longer, and
answer a request to delete it.

A conversation is personal data. The record of one is spread over as many
artifacts as the deployment keeps — the receipts of what was written, the audit
events of how it was decided, the session state that carried it from turn to
turn, the judgments a person left on its answers, the preferences learned from
those judgments and, where tracing is on, the spans naming it — and anything
said about retention has to be true of all of them at once. A window that
expires the events and leaves the receipts has retained the conversation, and so
has one that leaves the feedback file.

The first three are the account itself and are always there. The others belong
to layers above retention, so a removal takes them as stores the caller hands
in: each answers to a session identifier and reports how much went, and one
nobody hands in is one this deployment does not keep.

Two operations are required, and they are not the same operation.

```text
forget(session)          # a request, answered whatever the window is
expire(older_than_days)  # the window, applied to every session past it
```

Both remove the session from every store that holds it. Both leave a typed
`retention` event behind, naming the session and the counts removed per store
and carrying nothing that was said, read or written: a deletion nobody can see having happened is
indistinguishable from a file somebody edited. That event survives the removal
it records, and survives a later removal of the same session.

A window of zero keeps everything, which is the default, because a standard that
silently deleted an operator's record of what their system did would be worse
than one that kept too much. Deletion on request does not depend on the window
and is available whatever it is set to.

The rewriting of an append-only file is the one dangerous moment in this, and it
is handled the way such things are handled: the replacement is written beside
the original and renamed over it, so a reader sees the whole trail or the whole
of its replacement and never half of either.

The window is the second answer, not the first. What must not be kept is kept
out of the record in the first place: a field that must not be stored is not
written and then scrubbed, it is never written. Scrubbing is a promise about
every future writer of every future event; not writing is a property of the
record. Retention exists for what is stored because the account genuinely needs
it, and says how long that is.

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
  trail, and no behavior depends on a trace being recorded.
- Tracing is off unless configuration turns it on, and the conformance suite
  runs with it off.
- A turn spends no more model calls than its declared budget, and a turn that
  reaches it still answers.
- What must not be kept is not written. Removing it afterwards is a repair, not
  a design.
- The record of one session is removed from every store that holds it —
  receipts, audit trail, session state, and whatever else a deployment keeps
  under that session — together, or it is not removed.
- A removal leaves a typed event naming counts per store and nothing that was
  said, and that event survives the removal it records.
- An append-only trail is replaced whole: a new file, renamed over the old one.

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
- **OSKA-XC-15** — A deployment MUST be able to delete the whole record of one
  session on request. The deletion MUST remove that session's receipts, audit
  events and stored state, MUST NOT depend on a configured window, and MUST NOT
  remove the record of any other session.
- **OSKA-XC-23** — The deletion and the window MUST reach every other store the
  deployment keeps under a session identifier, including the feedback a person
  left on its turns, the preferences derived from that feedback and the trace
  spans of its turns where tracing is on. Each such store MUST report what it
  removed, those counts MUST appear in the report and in the `retention` event,
  and the event MUST still carry no content.
- **OSKA-XC-16** — A retention window MUST be configurable in days, MUST be
  measured from the last activity of a session, and MUST default to keeping
  everything. Applying it MUST remove from every one of those stores exactly the
  sessions past it.
- **OSKA-XC-17** — Every removal MUST emit a typed `retention` audit event
  recording the counts removed per store, including the stores of
  OSKA-XC-23. That event MUST NOT carry any content
  of the removed record, and MUST NOT itself be removed by the removal it
  records or by a later one.
- **OSKA-XC-18** — Rewriting an append-only record MUST be atomic from a
  reader's point of view: the replacement MUST be written to a new file and
  renamed over the original, and an entry that cannot be parsed MUST be kept
  rather than dropped.
- **OSKA-XC-19** — Data that a deployment must not retain MUST NOT be written to
  the record in the first place. An implementation MUST NOT rely on removal
  after the fact to satisfy a rule about what may be stored.
- **OSKA-XC-20** — A conformant deployment MUST be able to produce, over a
  stated window: the number of turns, turn latency at the median and at the
  95th percentile, the fallback rate, the verification failure rate, the
  guardrail block rate and the unsupported rate.
- **OSKA-XC-21** — A number that the deployment did not record MUST be reported
  as not computed, naming why, and MUST NOT be reported as zero. A number that
  is computed but does not cover everything its name suggests MUST say what it
  leaves out.
- **OSKA-XC-22** — These numbers MUST be derived from the audit trail and the
  traces alone, MUST NOT require any additional recording in the path of a turn,
  and reading them MUST NOT write to either source.

## Rationale

Configuration as an external document is the least interesting of the four and
the one that decides whether the other three are true. A threshold compiled into
the code is a behavior nobody can inspect and no reviewer can diff. Discovery's
minimum score is the difference between answering a request and declining it;
the fast-path threshold decides whether a model is consulted at all. Those are
policy decisions in the ordinary sense, and they belong in a file that an
operator can read, change and put under review, next to the connector bindings
and the policy documents.

Observability here is deliberately three artifacts rather than one log. A log
answers "what happened" and is terrible at the other two questions. Receipts
exist because "what changed in the world" must be answerable without replaying
and re-interpreting a stream of events, and because the retry decision depends
on it — the ledger is the same object. Scenarios exist because neither of the
other two tells you whether the behavior is intentional. A recorded
conversation that fails when the sequence of model calls changes is the only
artifact that makes the turn algorithm a thing you can refactor.

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
- `src/opsdeck/governance/ledger.py` — durable receipts, and the one operation
  that takes a session's receipts out of them.
- `src/opsdeck/governance/retention.py` — `forget` and `expire`, over the three
  stores together, and the event each of them leaves.
- `src/opsdeck/cli/commands_forget.py` — the two commands an operator runs, and
  what each of them reports having removed.
- `src/opsdeck/contracts/` — the shared types, owned by no layer.
- `tests/scenarios/test_scenarios.py` — the scenario runner, including the check
  that the scripted model calls were consumed in the recorded order.
