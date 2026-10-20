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

A deployment may also send its spans to the collector its observability stack
already reads. What leaves the process is narrower than what the file holds:
names, nesting, timing and the identifiers, kinds, counts and flags of a turn,
never what was said, sent or answered. The file is still written, because it is
what the service-level numbers are read from and what forgetting a session
reaches; a collector is out of that reach, so how long it keeps what it received
is the deployment's to set.

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

A draft from a session is a turn of its own for the budget: it opens a fresh
count, its model calls are counted against the per-turn limit like any turn's,
and a deployment whose turns may not call the model drafts from the recorded
operations alone.

Every cost bound has a floor, enforced where the configuration is read, a
tenant fragment included, so a value that would mean nothing, or would read as
no limit, is refused rather than served: the model budget, the conversation
window, a connector's timeout and response size, the breaker, retries,
backoff and recovery, the metrics interval and the platform's cache size.

### Identity

Responsibility: carry the actor.

OSKA assumes an authenticated channel. The engine does not authenticate
anyone; a channel either takes the identity an authenticating proxy sets or
verifies a signed session token itself ([06-governance-layer.md](06-governance-layer.md)).
What it requires is that the two human acts — approving a write and
transitioning a proposal — carry an actor identifier that is recorded and never
defaulted, together with where that identifier came from: the principal of the
call, the operator at a terminal, or a name the call only stated, which is
recorded as unverified. A deployment that puts an unauthenticated channel in
front of the engine has disabled the guarantees of L4 and L7 regardless of what
the engine does, and the documentation of a conformant system must say so
plainly.

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

### Metrics exposition

Responsibility: render the service-level numbers in a format a monitoring
system can scrape, without a client library to produce a handful of lines of
text.

```text
GET /metrics -> Prometheus text exposition format (version 0.0.4)
  oska_turns                      gauge, present while an audit trail exists
  oska_latency_p50_ms             gauge, present only while traces exist
  oska_latency_p95_ms             gauge, present only while traces exist
  oska_fallback_rate              gauge
  oska_verification_failure_rate  gauge
  oska_guardrail_block_rate       gauge
  oska_unsupported_rate           gauge
```

Every name carries the `oska_` prefix. Every number is a gauge over the report
window, the turn count included: it is recomputed from the audit trail, and
removing a session from the trail lowers it, so it is not a counter. A
deployment serving one catalog labels nothing, having nothing to distinguish a
series by; a platform labels every series with `tenant` and `domain`, one
series per (tenant, domain), each computed from that pair's own trail. A
computed report is served again for `metrics.cache_seconds` (15 seconds by
default) before the trail is read anew, because the trail is read in full to
compute one; zero reads it on every scrape. A report names every tenant a
platform serves and how much each one does, so where an identity is required
the route is a reviewer's read, like the proposals, unless the configuration
marks it public (`metrics.public`) for a scraper that reaches the runtime over
a private network and presents no identity; where no identity is required it
stays open, as every other read there is. A measure the deployment
cannot compute -- the same `Measure` the service report already carries -- is
left out of the body rather than exported as zero, for the reason the service
report gives that rule everywhere else it applies: a rate reported as zero
because nothing recorded it reads exactly like a rate that is genuinely zero,
and a scrape cannot tell the two apart from a number alone. Its `# HELP` line
still prints, naming why.

`ops/slo.md` sets one target per metric, with the reasoning behind it in two
sentences; `ops/alerts.yml` carries one alerting rule per target, against the
same metric names. The names the exposition renders, the names `ops/slo.md`
sets targets for, and the names `ops/alerts.yml` watches are the same set,
which a test checks so that changing one without the others fails the build
rather than a page nobody sends.

### Catalog precision and recall

Responsibility: gate a catalog change on whether it still routes correctly,
not only on whether the cases it was given happen to pass.

An evaluation set beside a skill is a list of cases, each a request and
whether it must reach that skill (`this`) or must not (`not this`). Run
against a ranking, every case becomes one of three counts: a `this` case that
held is a true positive; a `this` case that did not is a false negative,
whatever it lost the request to; a `not this` case that did not hold is a
false positive, because the skill was ranked first for a request that must
not reach it. Precision is true positives over true positives and false
positives; recall is true positives over true positives and false negatives.
Both are computed per skill and over the whole catalog run.

```text
EvaluationConfig:
  min_precision: float   # default 1.0
  min_recall: float      # default 1.0
  require_sets: bool     # default false
```

A catalog evaluation fails when precision or recall falls below the
configured floor. When `require_sets` is set, it also fails on any published
procedure -- a skill that declares triggers, so a conversation can be routed
to it -- that carries no evaluation set of its own, naming every one it
finds.

### Retention

Responsibility: keep the record for as long as it is needed and no longer, and
answer a request to delete it.

A conversation is personal data. The record of one is spread over as many
artifacts as the deployment keeps — the receipts of what was written, the audit
events of how it was decided, the session state that carried it from turn to
turn, the judgments a person left on its answers, the preferences learned from
those judgments, where tracing is on the spans naming it, and the proposals
drafted from it with the change requests composed from those — and anything
said about retention has to be true of all of them at once. A window that
expires the events and leaves the receipts has retained the conversation, and so
has one that leaves the feedback file.

The first three are the account itself and are always there. The others belong
to layers above retention, so a removal takes them as stores the caller hands
in: each answers to a session identifier and reports how much went, and one
nobody hands in is one this deployment does not keep. A proposal, and a change
request composed from it, is not always removed outright: one whose change was
applied is part of the catalog, so its package and the fields drafted from the
conversation are removed or redacted and the record of the decision stays.

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

On a platform ([14-platform-blueprint.md](14-platform-blueprint.md)) the state
of each (tenant, domain) lives apart, and a session id is not known in advance
to belong to one pair. A request to be forgotten concerns a person, not a
domain, so on a platform both operations reach every pair the platform serves
unless the operator narrows the reach to a domain or a tenant. Each pair is
opened exactly as the runtime opens it, with its own settings and its own
storage namespace, and the report sums the counts over the pairs reached and
names the pairs that held anything. The improvement job, which reads what
retention removes, runs the same way: once per pair, learning from that pair's
signals alone and writing only into that pair's state.

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
- A metric the exposition cannot compute is omitted from the scraped body,
  never exported as zero; every exposed name carries the `oska_` prefix.
- The metric names the exposition renders, the targets in `ops/slo.md` and the
  alerting rules in `ops/alerts.yml` are the same set.
- A catalog evaluation reports precision and recall separately: a request
  stolen from the procedure that must answer it and a procedure wrongly
  claiming a request it must not answer are different failures, not one
  blended score.

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
- **OSKA-XC-32** — Forget and expire MUST remove, or redact the
  history-derived fields of, proposals drafted from the session and the change
  requests composed from them. An applied change request MUST keep the record
  of its decision. Both MUST be counted in the report and in the retention
  event.
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
- **OSKA-XC-24** — A conformant deployment MUST be able to expose the numbers
  of OSKA-XC-20 in Prometheus text exposition format, on a route gated by
  configuration. Where an identity is required, the route MUST require a
  reviewer principal unless configuration explicitly marks it public.
- **OSKA-XC-25** — Every exposed metric name MUST be prefixed `oska_`. A
  metric the deployment could not compute MUST be omitted from the scraped
  body rather than exported as a zero value, and its `# HELP` line MUST still
  print, naming why.
- **OSKA-XC-26** — A platform exposition MUST label every series with
  `tenant` and `domain`, one series per (tenant, domain), leaving out only a
  pair whose tenant turns metrics off; a single-catalog deployment's samples
  MUST carry no label.
- **OSKA-XC-27** — The metric names the exposition renders, the targets
  declared for them and the alerting rules watching them MUST be the same
  set, and a conformant deployment's test suite MUST fail when they are not.
- **OSKA-XC-28** — A catalog evaluation MUST compute precision and recall
  separately, per skill and over the catalog as a whole, from true positive,
  false positive and false negative counts as defined under Catalog precision
  and recall.
- **OSKA-XC-29** — A catalog evaluation MUST fail when precision or recall
  falls below the configured floor, and, when configured to require a set of
  every procedure, MUST also fail on any published procedure that carries
  none, naming each one it finds.
- **OSKA-XC-30** — A conformant deployment's continuous integration MUST run,
  on every change proposed against it: static checks, the unit and scenario
  suites, catalog validation, the catalog evaluation gate of OSKA-XC-29, and
  conformance, all offline and without secrets.
- **OSKA-XC-31** — On a platform, forgetting a session and expiring the window
  MUST reach every (tenant, domain) the platform serves unless the operator
  narrows the reach to a domain or a tenant: a request to be forgotten concerns
  a person, not a domain. Each pair MUST be opened with its own settings and
  storage namespace, exactly as the runtime opens it. The report MUST sum the
  counts across the pairs reached and name the pairs that held anything. The
  improvement job run on a platform MUST run once per (tenant, domain) reached,
  learn only from that pair's signals, and write its learned files, preferences
  and report only into that pair's state.
- **OSKA-XC-33** — The turn count MUST be a gauge over the report window
  (`oska_turns`), omitted with a note when there is no audit trail.
- **OSKA-XC-34** — The interval a computed service report is served again MUST
  come from configuration (`metrics.cache_seconds`, default 15 s); zero MUST
  recompute on every scrape.
- **OSKA-XC-35** — Every cost bound MUST have a floor enforced where
  configuration is read, tenant fragments included.
- **OSKA-XC-36** — A conformance run MUST write only into storage namespaces
  it creates for that run, one per scenario, and MUST remove everything it
  wrote there (sessions, receipts and write claims) when each scenario ends,
  whether it passed or failed. It MUST NOT touch a namespace it did not create,
  the default namespace and every (tenant, domain) namespace included, so a
  suite run against a database a deployment shares leaves that deployment's
  record as it found it and leaves nothing of its own behind.
- **OSKA-XC-37** — Exporting diagnostic spans to an external collector MUST be
  off unless configuration selects it, and selecting it MUST NOT stop the file
  of OSKA-XC-12 from being written. An exported span MUST carry only its name,
  its nesting, its timing and the identifiers, kinds, counts and flags of the
  turn it belongs to; it MUST NOT carry a payload, the text a person sent or the
  text a model produced, and it MUST NOT carry the session identifier unless
  configuration asks for it, because what a collector holds is out of reach of
  forgetting. Credentials for the collector MUST come from the environment and
  MUST NOT be accepted in the configuration file.

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

A number scraped by a monitoring system is read the same way a number printed
by a person is read, and the zero-or-absent distinction that governs the
service report governs its exposition for the same reason: a dashboard that
cannot tell "nothing happened" from "nothing was recorded" pages the wrong
person or nobody at all. Writing the exposition by hand rather than importing
a client library keeps that rule visible at the one place it has to hold,
instead of trusting a dependency to have made the same choice. Keeping the
target and the alert beside the metric name, and failing the build when the
three drift, is the same discipline OSKA-XC-3 already applies to a threshold
in code: a number nobody is watching is not being monitored, whatever the
exposition says.

Splitting a catalog evaluation into precision and recall rather than one pass
rate makes two different mistakes visible as two different numbers. A
procedure that steals a request lowers the recall of the one it stole from,
not its own precision; a procedure that wrongly answers a request that is not
its own lowers its own precision, not anyone's recall. A blended score can
sit still while one axis degrades and the other improves to compensate, which
is exactly the failure a gate exists to catch.

## In the reference implementation

- `src/oska/config.py`, `configs/runtime.json`, `configs/model.json` —
  settings and the tunable values.
- `src/oska/cli/main.py` — `oska config`, which prints the settings in
  force after the environment has been applied, with secrets masked.
- `src/oska/audit/sink.py` — memory, file and fan-out sinks.
- `src/oska/audit/trace.py` — the diagnostic span sinks: none, memory and
  file, with the null sink in force whenever tracing is off.
- `src/oska/audit/otlp.py` — the file sink that also exports each span over
  OTLP, and the list of span fields allowed to leave the process.
- `src/oska/engine/budget.py` — the per-turn model budget and the provider
  wrapper that counts every call through it, held by `src/oska/engine/engine.py`
  for the length of a turn.
- `src/oska/governance/ledger.py` — durable receipts, and the one operation
  that takes a session's receipts out of them.
- `src/oska/governance/retention.py` — `forget` and `expire`, over the
  receipts, the audit trail and the session state together and every other
  store the caller hands in, and the event each of them leaves.
- `src/oska/learning/forgetting.py` — the proposals drafted from a session and
  the change requests composed from them, as retention removes them: deleted,
  or, for an applied change, redacted with the record of the decision kept
  (`tests/unit/test_retention.py`).
- `src/oska/cli/commands_forget.py` — the two commands an operator runs, and
  what each of them reports having removed; with `--platform` they reach every
  (tenant, domain), narrowed by `--domain` and `--tenant`, and
  `src/oska/cli/commands_improve.py` runs the improvement job per pair the same
  way (`tests/unit/test_retention.py`, `tests/unit/test_improvement.py`).
- `src/oska/contracts/` — the shared types, owned by no layer.
- `tests/scenarios/test_scenarios.py` — the scenario runner, including the check
  that the scripted model calls were consumed in the recorded order.
- `src/oska/observability/exposition.py` — the Prometheus text exposition,
  hand-written, and the metric name set the other two files are checked
  against.
- `src/oska/api/metrics.py` — the `GET /metrics` route, gated by
  `MetricsConfig.enabled` and mounted by `oska.api.app`, one labeled series per
  (tenant, domain) on a platform, each report kept for
  `MetricsConfig.cache_seconds`.
- `ops/slo.md`, `ops/alerts.yml` — the target and the alerting rule for each
  metric the exposition renders.
- `src/oska/catalog/evaluation.py` — precision and recall per skill and over
  the catalog, and the gate `EvaluationConfig` sets on both.
- `src/oska/cli/commands_evaluate.py` — `oska evaluate catalog`, which applies
  that gate and leaves a non-zero status when it does not clear.
- `tests/unit/test_metrics.py` — checks the exposition, `ops/slo.md` and
  `ops/alerts.yml` name the same metrics.
- `.github/workflows/ci.yml` — lint, the unit and scenario suites, catalog
  validation, the evaluation gate and conformance, run on every push and pull
  request against both reference catalogs.
