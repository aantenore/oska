# 06 Governance layer (L4)

## Purpose

L4 is the boundary between a decision and an effect. Reads cross it on their
declaration alone. Writes cross it only after a fixed sequence of checks, and
only with a receipt on the other side.

The layer exists so that the answer to "could this system have done X without
anyone knowing" is structural rather than procedural. There is one path from a
plan to a write, it performs these steps in this order, and no configuration
shortens it.

## The write path

The steps and their order belong to this layer. Running them does not. The
sequence is driven by the interaction layer, which holds the turn, the session
and the plan the write sits inside; in the reference implementation that driver
is `src/opsdeck/engine/execution.py`, with the second policy evaluation and the
approval arithmetic in `src/opsdeck/engine/engine.py`. Governance decides what
each step answers; interaction decides when each step is reached. An
implementation MAY place the driver elsewhere, and MUST NOT let it reorder the
path or skip a step.

```mermaid
flowchart TD
    A["Planned write operation"] --> B{"Declared by the integration?"}
    B -->|no| X1["outcome: invalid plan<br/>nothing runs"]
    B -->|yes| C{"Listed in the procedure's writes?"}
    C -->|no| X1
    C -->|yes| D{"Ledger key already<br/>recorded this session?"}
    D -->|yes| X2["outcome: ledger hit<br/>return the original receipt"]
    D -->|no| E{"Policy decision"}
    E -->|deny| X3["outcome: denied<br/>with the rule's reason"]
    E -->|allow / require second approval| F["Freeze exact payload,<br/>hash it, hold the plan"]
    F --> G["Reply with the exact payload<br/>and stop the turn"]
    G --> H{"Human decision"}
    H -->|reject or cancel| X4["outcome: rejected or cancelled<br/>nothing runs"]
    H -->|approve| I{"Hash still matches?"}
    I -->|no| X4
    I -->|yes| J{"Approvals given ==<br/>approvals required?"}
    J -->|no| G
    J -->|yes| K["Re-evaluate policy on<br/>current entity state"]
    K -->|deny| X3
    K -->|allow| L["Record intent in the ledger<br/>status pending, unverified"]
    L --> M["Execute through the connector"]
    M --> N["Update receipt:<br/>executed or error, response"]
    N --> O["Read back with the declared<br/>verification operation"]
    O --> P["Update receipt:<br/>verified / unverified / failed"]
    P --> Q["Resume the plan after this operation"]
    Q --> R["Reply from verified outcomes only"]
```

Every box on that path emits an audit event.

## Modules

### Capability Registry

Responsibility: answer whether an operation is declared, whether it writes, and
whether the procedure that planned it is allowed to perform it.

```text
CapabilityRegistry:
  operation_spec(integration_id, operation) -> OperationSpec | null
  is_declared(integration_id, operation) -> boolean
  is_write(integration_id, operation) -> boolean
  writes_allowed_by(procedure, operation) -> boolean
```

### Policy Engine

Responsibility: decide what happens to one write, given the payload and the
entity records the plan has already read.

A policy document belongs to one procedure and holds ordered rules. The first
matching rule decides. No matching rule denies. No policy at all denies. Reads
are not subject to policy; they are gated by declaration alone.

```text
PolicyRule:
  operation: string | "*"
  effect: allow | deny | require_second_approval
  when: map<dotted-path, {operator: value}>   # eq ne in not_in lt lte gt gte
  max_per_session: integer | null
  reason: string

PolicyEngine:
  evaluate(procedure, operation, payload, entities, session_id) -> PolicyDecision
```

Condition paths resolve against a merged view of the payload and the entity
records read in this turn, so a rule may say "not in production" by naming a
field of the entity rather than a field the model supplied.

### Approval Gate

Responsibility: freeze exactly what will be sent, and hold it until a human
decides.

```text
PendingApproval:
  approval_id, procedure_id, plan, op_index
  integration_id, operation
  exact_payload: object
  payload_hash: string        # over a canonical serialisation
  approvals_required: integer
  approvals_given: integer
  summary: string             # includes the exact payload

ApprovalGate:
  request(session, operation, payload) -> PendingApproval
  check(pending, payload) -> boolean
```

The hash is taken at request time and is independent of key order. Any change
to the payload produces a different hash and therefore requires a new request.
`check` is run twice before the write goes out: once against the payload the
held plan carries for the approved operation, and once against the frozen copy.
Comparing the frozen copy with itself would prove nothing, so the plan is the
other side of the comparison.

### Verifier

Responsibility: decide whether a write actually happened, by reading it back
through the operation the integration declared.

```text
Verifier:
  verify(spec, write_payload, connector) -> (status, evidence)
```

The read-back payload is built from the write payload, keeping only the fields
the read operation's input schema accepts. Each `expect` entry resolves either
to a literal or to a field of the write payload, and is compared against the
read-back result by dotted path. The status is `verified` only when the
read-back succeeded and every expectation held; otherwise `failed`, or
`unverified` when no read-back could run at all. The evidence is stored on the
receipt.

The entity the operation is bound to takes part in the comparison. When that
entity declares a state field and the closed set of states it may hold, a
declared transition supplies the expected value for that field unless the
`expect` block already names it, and a read-back that returns a value outside
the declared set is a failure whatever the other expectations said. See
[04-ontology-layer.md](04-ontology-layer.md).

### Write Ledger

Responsibility: make a write recoverable and non-repeating.

```text
WriteReceipt:
  receipt_id, session_id, ledger_key
  integration_id, operation, payload
  response: object | null
  status: pending | executed | error
  verification: verified | unverified | failed
  verification_evidence: object | null
  created_at, error

WriteLedger:
  key(integration_id, operation, payload, idempotency_fields) -> string
  record(receipt) -> WriteReceipt
  update(receipt_id, ...) -> WriteReceipt
  lookup(session_id, key) -> WriteReceipt | null
  count(session_id, operation) -> integer
  for_session(session_id) -> [WriteReceipt]
```

The intent is recorded before the connector is called, with status `pending`.
A write whose outcome is unknown — the call failed, or the read-back did not
confirm — stays in the ledger and blocks a second attempt on the same key.
`count` counts every receipt regardless of status, so a failed attempt still
consumes a session limit.

### Guardrails

Responsibility: hold the rules the system promises to keep, once, where they can
be read, and say of each one whether code keeps it or a model is merely told
about it.

A guardrail is declared in a governed catalog and referenced by a skill through
its identifier. The promise is written once; a skill that wants it names it and
restates nothing. An identifier no entry defines is an error, both when a skill
is admitted to the catalogue and when the runtime resolves what a skill
declares, because a guardrail nobody defined is a promise nobody keeps.

```text
Guardrail:
  id: string
  title: string
  description: string          # the promise in prose; for an instruction, the text itself
  enforcement:
    point: input | planning | execution | output
    mechanism: check | instruction | invariant
    validator: string | null   # required for a check: a check the runtime registers
                               # forbidden for an instruction and for an invariant
    module: string | null      # where the promise is kept; required for an invariant
    parameters: object         # everything the check reads: patterns, sets, thresholds
                               # declared by a check alone; nothing else reads them

GuardrailCatalogue:
  load(path) -> GuardrailCatalogue
  get(id) -> Guardrail | null
  ids() -> [string]
  for_skill(skill) -> [Guardrail]          # an undefined identifier is an error
  instructions(skills, point?) -> [string] # the prose of the instruction guardrails
  checks(point) -> [(Guardrail, Check)]

Check:
  (text_or_payload, context) -> (ok, reason, redacted_text | null)

GuardrailChain:
  apply_input(text) -> GuardrailOutcome    # before the turn is classified
  apply_output(text) -> GuardrailOutcome   # before the reply is returned
```

The three mechanisms are not three ways of saying the same thing. A `check` is
code the chain runs at the point the entry names and that can stop what it is
looking at; a refusal ends the turn with an outcome and an audit event, in the
words of the guardrail itself. An `invariant` is a promise kept elsewhere: a
named module holds it, at a point the chain runs nothing, and the entry says
which module so a reader can go and see. An `instruction` is text handed to the
planner or the reply writer and nothing more: it may be ignored by the model,
and the catalogue says so by declaring it an instruction. A catalogue that lets
one of these look like another is documentation pretending to be control, which
is worse than no catalogue, because it is believed.

The chain runs at two points, `input` and `output`, so those are the only
points at which a `check` means anything. A `check` declared at `planning` or at
`execution` names a validator nothing calls and carries parameters nothing
reads: its entry reads as enforcement while enforcing nothing, and editing its
parameters changes nothing at all. Such an entry is refused when the catalogue
is read. A promise kept at one of those points is declared an `invariant`
instead, which names the module that keeps it and nothing else — no validator
to suggest the chain runs it, no parameters to suggest they are read.

What a check reads belongs to its entry rather than to the code that runs it.
A pattern list, a closed set of allowed values or a threshold is part of the
promise and has to be reviewable next to the sentence that describes it. The
converse holds too: an entry with no validator to read them declares no
parameters, because a value in a catalogue is a promise that something reads
it.

### Audit

Responsibility: emit one typed event per step, to one or more sinks.

```text
AuditEvent:
  kind: turn | route | discovery | hydrate | plan | policy
      | approval_requested | approval_decided | execute | verify
      | ledger_hit | reply | proposal | guardrail | trace | budget
  session_id, turn_id, at, data
```

## Invariants

- Reads are gated by declaration. Writes are gated by declaration, policy,
  approval, ledger and verification.
- Default deny. A write with no policy loaded is denied.
- The approval freezes the exact payload. What is approved is what is sent,
  byte for byte after canonical serialisation.
- Policy is evaluated twice: when the approval is requested, and again after it
  is granted, against the entity state as it is at that moment.
- Intent is recorded before execution, never after.
- An identical write in the same session returns the original receipt and does
  not call the connector.
- A write is reported as successful only when its verification status is
  `verified`.
- Receipts survive a restart of the runtime.
- Every step emits an audit event, including the steps that refuse.
- A promise a skill declares is enforced by the chain at a named point, kept by
  a named module elsewhere, or handed to the model as text, and the catalogue
  says which of the three.
- A guardrail is declared once and referenced by identifier; an identifier no
  entry defines is an error and never a silent skip.
- What a check reads is part of its declaration, not of the code that runs it.

## Requirements

- **OSKA-GOV-1** — A write MUST NOT be executed unless its operation is declared
  by the integration and listed in the planning procedure's
  `capabilities.writes`.
- **OSKA-GOV-2** — A write MUST NOT be executed without a policy decision of
  `allow` or `require_second_approval`; the absence of a policy MUST deny.
- **OSKA-GOV-3** — Policy rules MUST be evaluated in order and the first
  matching rule MUST decide; no matching rule MUST deny.
- **OSKA-GOV-4** — A write MUST NOT be executed without a human approval
  recorded against the frozen payload hash for that approval.
- **OSKA-GOV-5** — The approval payload hash MUST be computed over a canonical
  serialisation and MUST be independent of key order. Before the write runs, the
  payload the held plan carries for the approved operation MUST be hashed and
  compared with the approval; so MUST the frozen copy. A difference on either
  side MUST stop the write and MUST require a new approval, because the plan
  and the frozen payload are both stored with the session and either can move
  while the approval waits.
- **OSKA-GOV-6** — A policy effect of `require_second_approval` MUST require two
  distinct approval turns before execution, and the system MUST state how many
  approvals stand.
- **OSKA-GOV-7** — The policy MUST be re-evaluated after approval and before
  execution, against the entity state read at that time; a denial at that point
  MUST stop the write.
- **OSKA-GOV-8** — The write intent MUST be recorded in the ledger with status
  `pending` before the connector is called.
- **OSKA-GOV-9** — After execution the receipt MUST be updated with the outcome
  and the connector response, and after verification with the verification
  status and its evidence.
- **OSKA-GOV-10** — Verification MUST use the read operation the integration
  declared and MUST NOT use the write's own response as evidence.
- **OSKA-GOV-11** — A write whose ledger key already exists for the session
  MUST NOT be executed again; the original receipt MUST be returned as the
  outcome.
- **OSKA-GOV-12** — `max_per_session` MUST be counted from the ledger over every
  receipt regardless of status or verification result.
- **OSKA-GOV-13** — A reply MUST NOT describe a write as successful or verified
  unless the receipt's verification status is `verified`.
- **OSKA-GOV-14** — Every governance step, including refusals, MUST emit a typed
  audit event carrying the session and turn it belongs to.
- **OSKA-GOV-15** — A guardrail MUST be declared once in a governed catalogue
  and referenced by identifier. A skill naming an identifier the catalogue does
  not define MUST be refused when it is admitted to the catalogue, and the
  runtime MUST treat such an identifier as an error rather than skipping it.
- **OSKA-GOV-16** — Every entry MUST declare the point at which it acts —
  `input`, `planning`, `execution` or `output` — and the mechanism that keeps
  it: `check` for code the guardrail chain runs and that can refuse,
  `invariant` for a promise a named module keeps where the chain runs nothing,
  `instruction` for text handed to a model. An entry MUST NOT be silent about
  either.
- **OSKA-GOV-17** — A `check` MUST name a validator the runtime registers, and
  every value it reads — pattern, set, threshold or word list — MUST be carried
  by the entry rather than written into the code that runs it. A validator the
  runtime does not register MUST be refused when the catalogue is read.
- **OSKA-GOV-18** — An `instruction` MUST NOT name a validator, MUST NOT carry
  parameters and MUST NOT be reported as enforcement. Its text MAY be handed to
  the planner or the reply writer; nothing else MAY depend on it being
  followed.
- **OSKA-GOV-19** — Guardrails declared at the `input` point MUST run before the
  turn is classified, and those declared at the `output` point before the reply
  is returned. A refusal MUST end the turn with an outcome, MUST emit an audit
  event of kind `guardrail` naming the guardrail that acted, and MUST answer in
  the words of that guardrail's own declaration.
- **OSKA-GOV-20** — A `check` MUST be declared only at a point the guardrail
  chain runs, and a catalogue declaring one anywhere else MUST be refused when
  it is read. A promise kept at a point the chain does not run MUST be declared
  an `invariant`, which MUST name the module that keeps it and MUST NOT name a
  validator or carry parameters. No entry MAY declare a validator or parameters
  that nothing reads, because an entry that reads as enforcement while nothing
  enforces it is the failure the catalogue exists to prevent: its parameters can
  be edited and nothing changes.

## Rationale

The obvious alternative is a permission list: mark some tools as dangerous and
ask the user to confirm before calling them. That is one step where the path
has seven, and it fails at each of the six it omits. A confirmation prompt
written by the model carries prose, while the connector carries arguments, so
approving "restart the search service" approves a sentence and leaves the
arguments unchecked. A tool that returns success reports that its request was
accepted, which is a weaker claim than that the world changed. A confirmed call
that times out leaves no record, so the natural human response — ask again —
issues a second write. And a confirmation that was correct thirty seconds ago
may be wrong now, because the entity it was judged against has moved.

Each step in the path answers one of those. The exact payload is frozen and
hashed, so approval attaches to the arguments themselves. Verification is a
declared read-back, so success is a fact about the world rather than a claim by
the system that caused it. The ledger records intent before the call, so an
unknown outcome is a known unknown and a retry is blocked instead of
duplicated. The second policy evaluation catches the state that moved while the
human was thinking.

Two of the rules cost something and are kept anyway. Default deny means a new
procedure that writes does nothing until someone writes its policy. The cost of
that falls on whoever authors the procedure, at a desk, with time to think,
instead of on whoever is holding the incident later. Counting failed attempts
against `max_per_session` means a genuine transient failure eats the budget;
the alternative — counting only successes — turns a session limit into no
limit at all, because the cheapest way to exceed it is to fail.

## In the reference implementation

- `src/opsdeck/governance/` — `capabilities.py`, `policy_engine.py`,
  `approval.py`, `verification.py`, `ledger.py`, `guardrails.py`.
- `configs/guardrails.json` — the declared guardrails, each naming the point it
  acts at, the mechanism that keeps it and the module that keeps it. Three of
  the entries are invariants: the read-back before a write is confirmed and the
  exact-payload approval are kept at `execution`, and planning from declared
  operations alone is kept at `planning`, all of them at points the chain does
  not run.
- `src/opsdeck/audit/` — `events.py`, `sink.py` (memory, file and fan-out
  sinks).
- `src/opsdeck/contracts/receipt.py`, `contracts/policy.py`,
  `contracts/events.py`.
- `src/opsdeck/engine/execution.py` — the interaction layer's executor, which
  sequences the path described above and refuses a write the declarations no
  longer cover by the time the approval comes back.
- `src/opsdeck/engine/engine.py` — the second evaluation, run against the state
  read after the approval was given: a refusal stops the write, and so does a
  rule that now asks for more approvals than stand.
- `schemas/policy.schema.json`, `spec/schemas/audit-event.schema.json`.
