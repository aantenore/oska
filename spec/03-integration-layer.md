# 03 Integration layer (L1)

## Purpose

L1 is the only path from the system to the world. It answers one question:
exactly which calls exist, and what does each one take and return. Everything
above it plans, governs and explains in terms of that list. Nothing above it
may construct a call the list does not contain.

The layer has two halves that must not be confused. The **operation registry**
is declarative: it is read from the catalogue and it is what the model, the
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
an error string when it did not, and how long it took. A connector never
receives free text, never receives a partial payload, and never decides whether
a result is good enough.

```text
Connector:
  id: string
  execute(operation: string, payload: object) -> ConnectorResult

ConnectorResult:
  ok: boolean
  data: object
  error: string | null
  latency_ms: integer
```

### Adapter kinds

Responsibility: provide concrete connector implementations. Four kinds cover
the field:

- **Mock.** Serves operations from a fixture held in memory, with declarative
  mappings from operation to collection, match field, projection and mutation.
  Supports injected failures so a conformance suite can exercise the unhappy
  paths deterministically.
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
- Binding is configuration. The same catalogue runs against a mock, an API or a
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
catalogue and the same recorded behaviour.

The narrow rules — argument vectors rather than shell strings, declared fields
only on HTTP, no free text — exist because L1 is where a mistake becomes an
effect. Every one of them removes a class of injection in which model output
reaches an interpreter. A model that emits a hostile string still cannot do
anything with it: the string must first pass a JSON Schema, then land in a
named field, then be substituted into a position the adapter chose.

## In the reference implementation

- `src/opsdeck/connectors/` — `base.py` (protocol and result), `mock.py`,
  `http.py`, `shell.py`, `registry.py`.
- `src/opsdeck/contracts/skill.py` — `OperationSpec`, `VerifySpec`.
- `src/opsdeck/governance/capabilities.py` — the operation registry view over
  the catalogue.
- `examples/ops/configs/connectors.json` — the fixture binding;
  `connectors.real.example.json` — the same catalogue bound to an HTTP API and
  a command-line tool.
