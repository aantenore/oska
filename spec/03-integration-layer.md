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
can be served by a mock in a test, an HTTP API in staging and a command-line
tool on an operator's laptop, with no change anywhere above L1.

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
  execute(operation: string, payload: object) -> ConnectorResult

ConnectorResult:
  ok: boolean
  data: object
  error: string | null
  latency_ms: integer
  failure: transport | refused | rejected | null
  classification: retryable | terminal | null
  health: ConnectorHealth | null
```

### Availability

Responsibility: tell "I cannot do that" apart from "I usually can, but not
right now", and stop calling a system that is not answering.

The connector is the only place that knows which of the two statements it is
making, so the distinction is part of its result and not a guess made above it.
A failure is one of three things:

- **transport** — the call did not reach the system, or the system did not
  complete it: a timeout, a refused connection, an address that is not there, a
  server error, a call the breaker refused. Retryable.
- **refused** — the system answered and the answer was no: a record that does
  not exist, a value it would not take, a permission denied. Terminal.
- **rejected** — the request was never sent, because it was not well formed:
  an undeclared operation, a payload missing a field the call needs. Terminal.

Every connector carries a breaker over that distinction. After a configured run
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
```

The three settings are configuration, read from the binding the same way for
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
  fields allowed in the query or body. Only declared fields are sent.
- **Command.** Maps each operation to an argument vector template with a
  timeout. Arguments are substituted positionally; a payload value is never
  concatenated into a command string and never interpreted by a shell.
- **Message or tool protocol.** Maps each operation onto a remote tool
  invocation. The operation name is the tool name.

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
  interpolate payload values into a string interpreted by a shell.
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
  `retryable`. A payload the system rejected, a record that does not exist, any
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

The narrow rules — argument vectors rather than shell strings, declared fields
only on HTTP, no free text — exist because L1 is where a mistake becomes an
effect. Every one of them removes a class of injection in which model output
reaches an interpreter. A model that emits a hostile string still cannot do
anything with it: the string must first pass a JSON Schema, then land in a
named field, then be substituted into a position the adapter chose.

## In the reference implementation

- `src/opsdeck/connectors/` — `base.py` (protocol, result, classification and
  breaker), `mock.py`, `http.py`, `shell.py`, `registry.py`.
- `src/opsdeck/config.py` — `ResilienceConfig`: the timeout, the run of
  failures that opens a breaker and the recovery window.
- `src/opsdeck/contracts/skill.py` — `OperationSpec`, `VerifySpec`.
- `src/opsdeck/governance/capabilities.py` — the operation registry view over
  the catalog.
- `examples/ops/configs/connectors.json` — the fixture binding;
  `connectors.real.example.json` — the same catalog bound to an HTTP API and
  a command-line tool.
