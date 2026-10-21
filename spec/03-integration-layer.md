# 03 Integration layer (L1)

## Purpose

L1 is the only path from the system to the world. It answers one question:
exactly which calls exist, and what does each one take and return. Everything
above it plans, governs and explains in terms of that list. Nothing above it
may construct a call the list does not contain.

The layer has two halves that must not be confused. The **operation registry**
is declarative: it is read from the catalog and it is what the model, the
policy engine and the auditor see. The **connector** is executable: it is bound
by configuration and it is what actually talks to a system. One operation name
can be served by a mock in a test, an HTTP API in staging, a command-line tool
on an operator's laptop or a tool on a protocol server another team runs, with
no change anywhere above L1.

## Modules

### Operation Registry

Responsibility: hold the closed set of declared operations and answer whether a
given operation exists, whether it writes, what payload it accepts, which
entity it acts on, how a write is read back, and which payload fields form its
idempotency key.

Interface: a lookup by integration identifier and operation name that returns
the declaration or nothing, plus predicates for "is declared" and "is write".
The registry is derived from integration skills (L3) and holds no state of its
own.

```text
OperationSpec:
  name: string                  # stable operation identifier
  write: boolean
  input_schema: JSON Schema     # the payload contract
  output_schema: JSON Schema
  verify: VerifySpec | null     # required when write is true
  idempotency_fields: [string]  # payload fields forming the ledger key

VerifySpec:
  operation: string             # a read operation of the same integration
  expect: map<string, string>   # dotted path -> literal or "$payload.<field>"
```

### Connector

Responsibility: execute one declared operation against one system and report
what happened, without interpreting it.

Interface: a single call taking an operation name and a validated payload, and
returning a uniform result: whether the call succeeded, the data it returned,
an error string when it did not, how long it took, and — when it failed — what
kind of failure it was and whether another attempt is sensible. A connector
never receives free text, never receives a partial payload, and never decides
whether a result is good enough.

```text
Connector:
  id: string
  health: ConnectorHealth
  execute(operation: string, payload: object,
          write?: boolean, idempotency_key?: string) -> ConnectorResult

ConnectorResult:
  ok: boolean
  data: object
  error: string | null
  latency_ms: integer
  failure: transport | refused | rejected | null
  classification: retryable | ambiguous | terminal | null
  status_code: integer | null
  health: ConnectorHealth | null
```

`write` and `idempotency_key` are what the caller knows and the connector
cannot see: whether the catalog declares the operation a write, and the key a
system that collapses repeated requests may recognize it by. A connector that
takes neither is called with the operation and the payload alone.

### Availability

Responsibility: tell "I cannot do that" apart from "I usually can, but not
right now", and stop calling a system that is not answering.

The connector is the only place that knows which of the two statements it is
making, so the distinction is part of its result and not a guess made above it.
A failure is one of three things:

- **transport** — the call did not reach the system, or it reached it and no
  usable answer came back: a refused connection, an address that is not there,
  a timeout, a connection lost after the request went out, a server error, a
  call the breaker refused. Retryable or ambiguous, as below.
- **refused** — the system answered and the answer was no: a record that does
  not exist, a value it would not take, a permission denied. Terminal.
- **rejected** — the request was never sent, because it was not well formed:
  an undeclared operation, a payload missing a field the call needs. Terminal.

A transport failure is split once more, by whether the request left:

- **retryable** — the request provably never left: the connection was refused,
  the name did not resolve, no pooled connection was free before it could be
  sent, the breaker kept it in. Nothing can have happened over there.
- **ambiguous** — the request left and no usable answer came back: a read that
  timed out, a connection reset after the request was sent, a server error, a
  command stopped after it started. The system may have applied it.

Where a connector cannot establish that a request never left, it says
`ambiguous`. The difference decides what happens to a write: a `retryable`
write is left to the ledger, which keeps its key open, and an `ambiguous` one
is settled by reading it back (06, the Write Ledger).

| What happened | HTTP | Command | Tool protocol | Classification |
|---|---|---|---|---|
| never sent: not well formed | undeclared operation, unfillable path | undeclared operation, bad argument | undeclared operation, unfillable argument | `terminal` (rejected) |
| never left | connection refused, name not resolved, pool exhausted, breaker open | executable not found, breaker open | server not started or reached, no answer to `initialize`, breaker open | `retryable` |
| left, no usable answer | read or write timeout, connection reset after send, 5xx | timed out after it started | tool call timed out, server gone or stream ended before answering, 5xx | `ambiguous` |
| the system said no | any 4xx, 409 included | non-zero exit | tool result with `isError`, protocol error for a malformed request, 4xx | `terminal` (refused) |
| answered past the budget | 2xx body over `max_response_bytes` | — | answer over `max_response_bytes` | `terminal` (refused) |

A read may be tried again inside one call. When its failure is `retryable`, or
its status is one the binding lists in `retry_statuses`, the connector repeats
it up to `retries` times, pausing `backoff_seconds` before the first repeat and
twice as long before each one after, and stops as soon as its breaker opens. A
write is never repeated inside a call: whether the catalog declares it a write
or its protocol method is not safe, it is sent once, and what happens next is
the ledger's decision.

A response is read as a stream and no further than `max_response_bytes`. A
successful answer larger than that is refused, `terminal`, with the budget
named in the error; an error answer is read only as far as the part of it the
error message keeps.

Every HTTP call whose method is not safe carries an `Idempotency-Key` header.
For a write the runtime executes, the key is derived from the session and the
ledger key of the write, so every attempt at one intent in one session carries
one key and no other intent carries it; a call made without one carries a key
derived from what it sends. The key is derived locally. It is useful only where
the system downstream honors it and collapses a request it has already seen,
and harmless where it does not.

Every connector carries a breaker over the three kinds of failure. After a configured run
of consecutive transport failures it stops calling the system: calls are
refused in the connector, without leaving the process, for the configured
recovery window. One attempt is then let through, and it closes the breaker or
opens it again. Only transport failures count towards it: a system that
answered is a system that is up, however it answered, and a request that was
never sent says nothing about the system at all.

```text
ConnectorHealth:
  state: closed | open | probing
  consecutive_failures: integer
  retry_in_seconds: integer | null

ResilienceConfig:
  timeout_seconds: number
  failures_before_open: integer
  recovery_seconds: number
  retries: integer                # in-call repeats of a read; 0 by default
  backoff_seconds: number         # the first pause; doubled for each repeat
  retry_statuses: [integer]       # statuses that allow a read to repeat
  max_response_bytes: integer     # the most of a body a connector reads
```

The settings are configuration, read from the binding the same way for
every kind of adapter, and an outage is bounded by them rather than by anything
written into an adapter.

### Adapter kinds

Responsibility: provide concrete connector implementations. Four kinds cover
the field:

- **Mock.** Serves operations from a fixture held in memory, with declarative
  mappings from operation to collection, match field, projection and mutation.
  Supports injected failures so a conformance suite can exercise the unhappy
  paths deterministically, and an injected outage is declared apart from an
  injected refusal, because the two are not the same failure.
- **HTTP.** Maps each operation to a method, a path template and the payload
  fields allowed in the query or body. Only declared fields are sent. A value
  put into the path is one percent-encoded segment and nothing more: it cannot
  climb out of its segment, start a query or cut the path short, and a value
  that names no segment at all is refused before anything is sent.
- **Command.** Maps each operation to an argument vector template with a
  timeout. Arguments are substituted positionally; a payload value is never
  concatenated into a command string, never interpreted by a shell, and never
  read as an option of the command.
- **Tool protocol.** Maps each operation onto one named tool of a server
  speaking a tool-calling protocol, the Model Context Protocol in the
  reference implementation, reached over its standard input and output or over
  HTTP. The binding declares the tool and a template of its arguments; the
  arguments are filled from the payload one value at a time, so a payload can
  neither add an argument nor rename one. The adapter is a client of the
  server and nothing more: the server's list of tools is never shown to a
  model and never consulted while a turn runs, so a tool the server offers and
  no skill declares cannot be reached. A server the adapter starts is started
  from a declared argument vector, never through a shell, and sees none of the
  environment beyond the search path and what the binding declares. The
  protocol carries no idempotency key, so an `ambiguous` write is settled by
  its read-back like any other.

### Connector Registry

Responsibility: bind integration identifiers to connector instances from
external configuration, and refuse to resolve an integration that is not bound.

```text
ConnectorRegistry:
  from_config(path) -> ConnectorRegistry
  get(integration_id) -> Connector        # raises when unbound
  has(integration_id) -> boolean
```

## Invariants

- An operation that is not declared does not exist. There is no dynamic
  operation, no passthrough and no escape hatch.
- Every operation has an input schema. A payload is validated against it before
  a connector sees it.
- Every write operation declares the read that verifies it, and that read
  belongs to the same integration.
- A connector receives structured payloads only. Natural language never reaches
  L1.
- A connector reports; it does not judge. Whether a write succeeded is decided
  by the verifier in L4 from the read-back, not by the connector's own result.
- A failure says whether another attempt is sensible. Nothing above L1 guesses
  it from an error message.
- A record that does not exist is not an outage. Only a failure to reach the
  system counts towards the breaker.
- A binding that is down takes its own operations with it and nothing else. The
  conversation continues, and the capability is reported as unavailable rather
  than as refused or as performed.
- Binding is configuration. The same catalog runs against a mock, an API or a
  command-line tool without any change above L1.

## Requirements

- **OSKA-INT-1** — Every operation reachable by the system MUST be declared with
  a name, a read or write flag, an input schema and an output schema.
- **OSKA-INT-2** — A connector MUST reject an operation name that is not
  declared for it, and MUST NOT attempt the call.
- **OSKA-INT-3** — A payload MUST be validated against the operation's input
  schema before the connector is called; an invalid payload MUST NOT be sent.
- **OSKA-INT-4** — Every operation with `write` true MUST declare a `verify`
  spec whose `operation` is a read operation of the same integration.
- **OSKA-INT-5** — Every operation MUST be bound to exactly one entity type
  published in the ontology.
- **OSKA-INT-6** — A connector MUST return a uniform result carrying success,
  data, error and latency, and MUST NOT raise for a remote failure it can
  describe.
- **OSKA-INT-7** — A command adapter MUST pass an argument vector and MUST NOT
  interpolate payload values into a string interpreted by a shell. It MUST
  refuse, without running anything, a payload value that would start an
  argument with `-`, unless the declared vector ends the command's options with
  `--` before that argument.
- **OSKA-INT-8** — An HTTP adapter MUST send only the payload fields the
  operation declares for the path, query and body.
- **OSKA-INT-9** — A connector MUST NOT receive free-form user text in any
  field the operation does not declare.
- **OSKA-INT-10** — The binding from integration identifier to connector MUST
  come from external configuration, and an unbound integration MUST be an error
  at resolution time, not at call time.
- **OSKA-INT-11** — An operation's `idempotency_fields` MUST name payload fields
  declared in its input schema; an empty list means the whole payload keys the
  write.
- **OSKA-INT-12** — A conformant system MUST provide at least one adapter that
  runs offline with injected failures, so the conformance suite needs no live
  system.
- **OSKA-INT-13** — Every connector MUST take the time one call may last from
  configuration, by the same setting for every kind of adapter. A call that
  exceeds it MUST be abandoned and reported as a failure, and MUST NOT raise.
- **OSKA-INT-14** — Every failed result MUST carry a classification saying
  whether another attempt is sensible. A timeout, a connection that was refused
  or lost, a server error and a call refused by an open breaker MUST be
  `retryable` or `ambiguous`, as OSKA-INT-20 decides between them. A payload
  the system rejected, a record that does not exist, any
  other refusal by the system, and a request that was never sent because it was
  not well formed MUST be `terminal`.
- **OSKA-INT-15** — A connector MUST hold a breaker. After the configured run
  of consecutive transport failures it MUST refuse calls without reaching the
  system for the configured recovery window, MUST then let exactly one attempt
  through, and MUST close on that attempt succeeding and open again on its
  failing.
- **OSKA-INT-16** — Only transport failures MUST count towards the breaker. A
  result the system produced, whether a success or a refusal, MUST close it; a
  request that was never sent MUST NOT count either way.
- **OSKA-INT-17** — A connector MUST report its health: the state of its
  breaker, the run of failures behind it, and when it may next be tried.
- **OSKA-INT-18** — A connector that is unavailable MUST degrade the operations
  of that binding alone. The runtime MUST report such an operation as
  temporarily unavailable, naming when it may be tried again, MUST NOT report
  it as refused and MUST NOT report it as performed, and MUST carry on with the
  rest of the conversation.
- **OSKA-INT-19** — The offline adapter MUST be able to simulate a transport
  failure, and that simulation MUST be declared apart from a simulated refusal
  by the system, so a conformance suite can exercise an outage without
  exercising a refusal.
- **OSKA-INT-20** — A transport failure MUST be classified by whether the
  request left. It MUST be `retryable` only where the request provably never
  left: a refused connection, a name that did not resolve, no pooled connection
  before the request was sent, a call the breaker refused. It MUST be
  `ambiguous` where the request left and no usable answer came back: a read or
  write timeout, a connection reset or lost after the request was sent, a
  server error. A connector that cannot establish that a request never left
  MUST classify it `ambiguous`. A refusal by the system, a 409 conflict
  included, MUST be `terminal`.
- **OSKA-INT-21** — A connector MAY repeat a read inside one call, at most the
  configured `retries` times, only when its failure is `retryable` or its
  status is listed in `retry_statuses`, pausing `backoff_seconds` before the
  first repeat and a growing pause before each later one, and MUST stop when
  its breaker opens. A write — an operation the catalog declares a write, or a
  call whose protocol method is not safe — MUST NOT be repeated inside a call.
- **OSKA-INT-22** — A connector MUST NOT read more of a response body than
  `max_response_bytes`. A successful answer larger than that MUST be refused as
  `terminal`, and the error MUST name the budget.
- **OSKA-INT-23** — An HTTP adapter MUST send an `Idempotency-Key` header with
  every call whose method is not safe. For a write the runtime executes, the
  key MUST be derived from the session and the ledger key of the write, so
  every attempt at one intent in one session carries the same key and no other
  intent carries it. The key is derived locally, and a conformant system MUST
  NOT rely on it where the downstream system is not known to honor it.
- **OSKA-INT-24** — An HTTP binding MUST percent-encode each payload value
  placed in the path as a single segment, and MUST refuse, without sending, a
  value that is empty, `.` or `..`.
- **OSKA-INT-25** — A tool-protocol binding MUST map each declared operation to
  exactly one named tool, and MUST call no other tool for it. It MUST NOT
  expose the server's list of tools to a model or consult it while a turn
  runs, and MUST reject an operation the binding does not declare without
  starting or contacting the server.
- **OSKA-INT-26** — A tool-protocol binding MUST build the arguments of a call
  from a declared template, filling each placeholder from one payload field. A
  payload value MUST NOT add, rename or remove an argument, a payload field
  the template does not name MUST NOT be sent, and a placeholder inside a
  longer string that the payload cannot fill with one value MUST be rejected
  without sending.
- **OSKA-INT-27** — A tool-protocol binding that starts its server MUST start
  it from a declared argument vector without a shell, and MUST NOT pass it
  the environment of the process beyond the search path and the variables the
  binding declares.
- **OSKA-INT-28** — A tool-protocol binding MUST classify a failure by whether
  the tool call was sent: a server that could not be started, reached or
  initialized MUST be `retryable`; a tool call that timed out, or whose server
  exited, ended its stream or reported a server error before answering, MUST
  be `ambiguous`; a tool result marked as an error, and a protocol error
  saying the request was malformed, MUST be `terminal`.
- **OSKA-INT-29** — When a pack is checked and a tool server it binds can be
  reached, an operation bound to a tool the server does not list MUST be
  reported as an error. A server that cannot be reached MUST be reported as
  not checked, and MUST NOT pass the check silently. A server started over
  standard input and output runs a command on the checking machine, and MUST
  be refused unless the operator allows commands, as a command binding is.

## Rationale

The obvious alternative is to hand the model a tool-calling interface and let
each tool define itself in its own docstring. That is faster to start and it
fails in three ways. First, the capability surface becomes whatever the tool
list currently is, so nothing above it can reason about the set as a whole: you
cannot ask "which operations write?" or "what verifies this?" because those
facts are prose. Second, verification has nowhere to live; a tool that returns
"ok" is believed. Third, the declaration and the implementation are the same
object, so swapping a real system for a fixture changes the contract the model
sees, and the test no longer tests the thing that ships.

Splitting the declaration from the execution costs one indirection and buys all
three back. The declaration is data, so the validator can cross-check it, the
policy engine can key on it, the planner prompt can be derived from it, and
the auditor can read it. The execution is configuration, so a conformance suite
runs against fixtures and production runs against the real system with the same
catalog and the same recorded behavior.

Binding operations to a tool server does not bring the tool-calling interface
back. The server's tools are what a system offers, the way an HTTP API's
endpoints are; the skill still says which of them exist for this system, with
schemas, an entity and a read-back, and the binding names one tool for each.
A tool the server adds tomorrow is invisible until a skill declares it, and a
tool it removes is caught by the pack check rather than by a failed turn.

The narrow rules — argument vectors rather than shell strings, declared fields
only on HTTP, no free text — exist because L1 is where a mistake becomes an
effect. Every one of them removes a class of injection in which model output
reaches an interpreter. A model that emits a hostile string still cannot do
anything with it: the string must first pass a JSON Schema, then land in a
named field, then be substituted into a position the adapter chose.

## In the reference implementation

- `src/oska/connectors/` — `base.py` (protocol, result, classification,
  breaker, in-call retries of reads, the idempotency key and `invoke`),
  `mock.py`, `http.py` (what left and what did not, the response budget, the
  `Idempotency-Key` header), `shell.py`, `mcp.py` (the tool-protocol client
  over stdio and streamable HTTP, its argument templates and its
  classification), `registry.py`.
- `src/oska/config.py` — `ResilienceConfig`: the timeout, the run of
  failures that opens a breaker, the recovery window, the in-call retries of a
  read and the response budget.
- `src/oska/contracts/skill.py` — `OperationSpec`, `VerifySpec`.
- `src/oska/governance/capabilities.py` — the operation registry view over
  the catalog.
- `examples/ops/configs/connectors.json` — the fixture binding;
  `connectors.real.example.json` — the same catalog bound to an HTTP API and
  a command-line tool.
- `src/oska/kit/check.py` — the tool check of `oska pack check`.
