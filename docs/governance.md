# Governance

Every change to the world goes through the same sequence. The sequence is in
the code, not in configuration: there is no setting that removes a step, and a
procedure with no policy writes nothing.

## The write path

A planned operation whose declared spec says `write: true` stops the plan. What
follows happens in `engine/execution.py` and `governance/`.

Before any of it, the whole plan is read once and the fields the procedure
declares under `principal_inputs` are filled from the identity the channel
authenticated, into every operation whose input schema accepts them. That is
the section on identity below; it happens here so that nothing — not a read,
not the policy, not the payload a person is shown — is ever built from a value
the model chose for a field the identity owns.

1. **Declared.** The operation must be declared by the integration the plan
   names. `CapabilityRegistry.operation_spec` looks it up; an operation that is
   not there ends the plan with an `invalid_plan` outcome. This check already
   ran for every operation of the plan at the planner, and runs again here,
   because the plan is data and data is checked where it is used.
2. **Allowed by the skill.** The operation must appear in
   `capabilities.writes` of the procedure being run. A plan that reaches for a
   write the procedure does not list is an `invalid_plan` outcome, even though
   the integration declares it.
3. **Ledger lookup.** The ledger key is computed and `settled` is asked for
   this session. A receipt that settles the intent emits `ledger_hit`, records
   a `ledger_hit` outcome with the original receipt, and moves on to the next
   operation. The write does not run twice. A previous attempt the connector
   classified `retryable` settles nothing — it never reached the system — so
   the intent may be attempted again.
4. **Policy.** `PolicyEngine.evaluate` loads the policy the procedure declares
   and applies the first matching rule to the payload merged with the entity
   records the plan has read so far. The effect is emitted as a `policy` event.
   `deny` ends the plan with a `denied` outcome carrying the reason.
5. **A legal move.** When the operation acts on an entity that declares a
   lifecycle, the current state of the object is read and the operation has to
   name a move that is legal from it. A move that is not there ends the plan
   with an `invalid_plan` outcome, before any approval is asked for. This is
   the section below.
6. **Approval.** `ApprovalGate.request` freezes the operation on the exact
   payload, hashes it, and stores a `PendingApproval` on the session, with
   `approvals_required` set to 2 when the policy said
   `require_second_approval` and 1 otherwise. An `approval_requested` event
   carries the approval id, the operation, the payload and the number required.
   The turn ends with an `approval_pending` outcome and a reply that shows the
   payload verbatim. Nothing has run.
7. **Intent recorded.** When the approval arrives and the hash still matches,
   steps 1 and 2 are run again over the catalog as it stands now, because the
   catalog is files on disk and the approval was given in an earlier turn. An
   operation that has since been withdrawn, turned into a read, or dropped from
   the procedure's writes ends the turn with an `invalid_plan` outcome and
   nothing in the ledger. What the plan read before the write is read again;
   the policy is evaluated against the entities as they are now, rather than as
   they were when the question was asked; and step 5 is made again against the
   state the object is in now, so a move that stopped being legal while the
   approval waited ends the turn the same way, with nothing in the ledger. When
   any of those reads cannot be performed again, the write is not sent either:
   a policy judging without the state it is conditioned on would silently stop
   matching, so the turn ends with an `unavailable` outcome saying the state
   could not be confirmed, and nothing in the ledger. Only
   then is a receipt written with status `pending`, before the call goes out.
   An `execute` event with `"stage": "intent"` carries the receipt id and the
   ledger key.
8. **Execute.** The connector is called with the operation name and the payload.
   A connector that raises is caught, and the failure becomes an outcome. An
   `execute` event with `"stage": "call"` records whether it succeeded and how
   long it took. The receipt is updated to `executed` or `error` with whatever
   the call returned, and carries the classification the connector gave the
   failure. A call that never reached the system takes the `unavailable` path
   instead, described below.
9. **Verify.** The read-back the integration declares is run and its result
   compared against `verify.expect`. A `verify` event carries the status and the
   evidence.
10. **Ledger update.** The verification status and its evidence are written to
    the receipt. The receipt is now the record of what happened.
11. **Reply.** The reply is built from the outcomes of the turn. When a model
    phrases it, the deterministic line of every receipt and every ledger hit is
    appended to what the model wrote, so the account of the write comes from
    the receipt even when the prose around it does not. A write that was not
    verified is reported as not verified, and the conformance runner fails a
    scenario whose replies never name a receipt, or name one as verified while
    the receipt says otherwise.

If a composite still has steps after the approved write, the plan continues from
the operation after it and the goal advances, so one approval unblocks the rest
of the sequence without unblocking any other write.

## The lifecycle of an object

Step 5 is the whole of what a declared lifecycle adds to the write path. An
entity declares the state it starts in, the states it never leaves, the read
its current state is taken from, and the moves between states with the
operation that causes each. [skill-contract.md](skill-contract.md) has the
fields; this is what the write path does with them.

**Where the current state comes from.** From the system of record, and from
nowhere else. The executor looks first at the entity records the plan has
already read this turn — a procedure that reads before it writes has usually
read exactly this — and otherwise calls the read the entity names in
`state_read`, with the write payload narrowed to the properties that read
declares. It is never taken from the conversation, and never from what the
model said the state was. A state that cannot be read at all is not a reason to
proceed: the plan ends with an `invalid_plan` outcome saying the state could
not be read through that operation, so the move could not be judged.

**What makes a move illegal.** The entity declares no move whose `from` holds
the state the object is in and whose `operation` is the one about to run. That
covers the two cases worth separating in the reply: a state the catalog can
move out of by some other operation, and a final state the catalog moves out of
by none. Either way the refusal names the state the object is in and lists the
moves that are legal from it, and no approval is requested. A procedure may
still plan the operation; what is refused is running it from here.

`required` and `offered` do not decide legality. `required` marks the move that
completes the object, and the resolver appends a note to it when it offers it.
`offered: false` marks a move that is legal and must never be suggested: the
runtime plans and runs it when a person asks, and never proposes it. Both are
about what is said, not about what is allowed.

**What verification checks that it would not otherwise.** The move carries the
state it promises. After the write, the read-back is compared against
`verify.expect` as always, and two further things are recorded on the evidence.
A state outside the entity's declared set fails the verification whatever the
other comparisons said, under `state_outside_declared_set`. And a read-back
that lands on a state other than the one the move promised is recorded under
`state_the_move_promised`, with the state expected and the state read. A write
that ran and left the object somewhere else is not a verified write.

## When the system does not answer

"I cannot do that" and "I usually can, but not right now" are different
statements, and the connector is the only place that knows which one it is
making. So every connector result carries two things: a `failure` kind saying
where in the transaction it went wrong, and a `classification` saying what
that means for a retry, the breaker and the ledger. `failure` alone was the
whole story once; a transport failure has since been split into two
classifications, because "nothing was ever sent" and "it was sent and the
answer is unknown" call for different handling, and conflating them is what
used to make a timeout look exactly like a refused connection.

| `failure` | What it means | `classification` |
| --- | --- | --- |
| `transport` | The request provably never left: a refused connection, a host that is not there, a breaker that is open | `retryable` |
| `transport` | The request left and no usable answer came back: a read timeout, a connection reset mid-response, a server reporting it is broken (5xx) | `ambiguous` |
| `refused` | The system answered and the answer was no: a record that does not exist, a value it would not take, a permission denied, a response larger than `resilience.max_response_bytes` | `terminal` |
| `rejected` | The request was never sent, because it was not well formed: an operation the binding does not declare, a payload missing a field the call needs, an unfillable path | `terminal` |

A result of unknown provenance — a connector that answers without classifying —
is read as `terminal`, so nothing is ever repeated on this system's own
initiative. Every adapter shipped with the runtime (`mock`, `http`, `shell`)
classifies every path; only `http` and `shell` can produce `ambiguous`, since
only they have a wire a response can go missing from — `mock` only ever
reports `retryable` (an injected `unreachable`) or `terminal`.

**In-call retries, reads only.** Before any of this reaches the write path, a
**read** whose result is retryable, or whose HTTP status is in
`resilience.retry_statuses`, is retried inside the same call, up to
`resilience.retries` more times, with the pause between attempts doubling from
`resilience.backoff_seconds`. A **write** is never retried inside a call,
whatever its classification — the loop that retries a read simply does not run
for one, so a write reaches the write path's own handling, below, on the first
answer it gets. The circuit breaker is consulted before every attempt of the
loop, not only the first, so a breaker that opens mid-retry stops the rest of
the attempts.

**The breaker.** After `resilience.failures_before_open` consecutive results
whose `failure` is `transport` — retryable or ambiguous, either counts — the
connector stops being called: calls are refused locally, without reaching the
system, for `resilience.recovery_seconds`. One attempt is then let through and
either closes the breaker or opens it again. A system that answered —
including one that refused, or one whose answer was too large to read — is a
system that is up, and closes it. A request that was never sent counts for
nothing. Only the operations of that one binding degrade; nothing else about a
conversation changes.

**What a `retryable` write does to the ledger.** A write whose request
provably never left settles nothing: the receipt stays `error` and
`retryable`, and the same intent may be asked for again in the same session,
because nothing over there was ever attempted.

**What an `ambiguous` write does to the ledger.** This is the harder case, and
nothing here is settled by assumption. The write's own read-back — the same
`verify.expect` comparison every write goes through — is run on it exactly as
it would be on one that reported success, because a failed send says nothing
about whether the system performed it anyway. What that read-back shows
decides which of three things happens next:

- **It shows the write landed.** The receipt becomes an ordinary executed,
  verified write, `classification: terminal`, and the key closes: the same
  intent is not sent again, because it does not need to be.
- **It shows the write did not land.** The receipt is reopened,
  `classification: retryable`, and the same intent may be asked for again —
  the system of record itself says nothing happened.
- **The read-back cannot say either way.** The outcome is genuinely unknown.
  If the operation declares `idempotency_fields`, or is a lifecycle move the
  entity would itself refuse to repeat from the state it would be in, the
  system of record would stop a duplicate on its own, so the receipt is
  reopened as `retryable` and a repeat is safe to ask for. Otherwise nothing
  stands between a repeat and a duplicate effect, so the key closes,
  `classification: terminal`, with the error naming the outcome as unknown —
  the runtime does not send a write again when it cannot tell whether the
  first one happened.

`WriteLedger.settled` is what the write path asks before sending anything, and
it is exactly this: a receipt classified `retryable` never settles a key, a
receipt classified `terminal` always does, whatever kind of failure or success
produced it. `lookup` returns the most recent receipt for a key regardless,
because what was tried is part of the account even when nothing came of it.

**Claiming a key is atomic across processes, not only within one.**
`WriteLedger.claim` opens a `BEGIN IMMEDIATE` SQLite transaction, checks
whether the key is already settled, and inserts the receipt in the same
transaction — so a second process asking for the same write blocks on the
file's write lock rather than racing the check. The PostgreSQL backend
(`docs/configuration.md#storage`) makes the same guarantee a different way: an
`INSERT ... ON CONFLICT DO NOTHING` guarantees a row to lock, `SELECT ... FOR
UPDATE` takes it, and the settlement check runs inside that row lock — replicas
queue on the row rather than on the whole ledger file. Either way, two turns,
two threads or two processes asking for the same write at once cannot both win
it.

**The `unavailable` outcome.** An operation whose call never arrived, or whose
outcome could not be settled either way, is neither a result nor a refusal. It
is recorded as an `unavailable` outcome naming the binding, the operation, the
state of the breaker and when a call may go out again, and an `unavailable`
audit event carries the same. The plan carries on past it. For a write, the
receipt of the attempt is carried on the outcome, so the reply names it along
with every other write of the session.

The reply says it twice over, deliberately. The deterministic line reports that
the operation could not be reached, that nothing was sent and nothing changed,
and when it may be tried again. The reply writer is separately instructed not
to say it was refused and not to say it was done, because those are the two
mistakes available here and both are worse than saying nothing. Where an
ambiguous write's outcome was actually resolved by its read-back — landed, or
did not land — the reply reports exactly that, the same as any other write:
the uncertainty was real but it did not last past this turn.

An outcome marked unknown — the write's own answer was lost and its read-back
could not be reached either — is never reported as failed. It is not sent
again, because the system of record would stop a duplicate on its own; the
deterministic line and the reply writer's instruction both say only that the
write may or may not have landed, name the receipt that records the attempt,
and say whether the runtime would send it again. An approved write whose
state could not be confirmed by a re-read (the previous section) is worded the
same way: not sent, not refused, not done, and askable again.

A model-written reply is trusted with this only when every write of the turn
landed and was confirmed. The reply writer is handed, per write, whether it
landed; the moment any write of the turn was refused, never sent, not
confirmed by its read-back, or unknown, the text a model returns is set aside
and the deterministic form answers instead — the call still runs, because it
is also what puts the offered actions into the turn's language, but its prose
is not used. See [docs/architecture.md#l6-interaction](architecture.md#l6-interaction) (`reply.py`).

## Guardrails

A guardrail is a rule this installation promises to keep. The promise is
written once, in a catalog document, and a skill references it by identifier —
so the wording cannot drift between the procedures that claim it, and a reader
can count the promises without reading the procedures.

### The catalog contract

`guardrails.catalog` in `configs/runtime.json` names the document, read from
the working directory and then from beside the runtime configuration.
The normative schema is `spec/schemas/guardrail-catalog.schema.json`. Each
entry is one identifier mapped to a title, a description and an enforcement
block:

```json
"guardrail.exact_payload_approval": {
  "title": "What was approved is what is sent",
  "description": "A write runs only with the payload a person was shown and approved ...",
  "enforcement": {
    "point": "execution",
    "mechanism": "check",
    "validator": "member_of_declared",
    "module": "oska.governance.approval",
    "parameters": {
      "field": "payload_hash",
      "declared": "approved_hashes",
      "reason": "the payload is not the one that was approved"
    }
  }
}
```

`point` is where the promise acts: `input`, `planning`, `execution` or
`output`. `module` names where in the package the promise is kept, so a reader
can go and see. `parameters` holds everything the check reads — no pattern, no
threshold and no word list is written into the code, which is why changing what
a guardrail refuses is an edit to this document.

### Checks and instructions

`mechanism` is the distinction the document exists for.

**`check`** means code runs at that point and can refuse or redact. The entry
names a validator the runtime registers, and a validator the runtime does not
register makes the catalog fail to load rather than fail quietly. Three are
registered: `forbidden_pattern`, which refuses text matching any declared
expression and hands back a redacted copy; `value_in_set`, which refuses a
payload whose declared field is outside a closed set; and `member_of_declared`,
which refuses a field that is not among the values the turn itself declares.

**`instruction`** means the description is handed to the model as text and
nothing enforces it. An instruction declares no validator, and declaring one is
an error.

A guardrail kept by a check is never also put in a prompt. The model is given
the instruction guardrails and only those, because handing it a rule the code
already enforces invites it to argue with the code. The planner reads them in
its system prompt, after its own rules; they are not appended to the
conversation.

The chain the engine runs covers the two points that are about the text of a
turn: the input checks run over what arrived before anything is decided, and
the output checks run over the reply before anyone reads it. The first
guardrail that refuses ends the chain, and what it says is what the turn
answers with; one that only redacts hands its copy to the next. The planning
and execution entries are declarations about code that was already there — the
plan validator, the approval gate, the verifier — and `module` is how a reader
finds it.

### What the shipped catalog declares

| Identifier | Point | Kept by |
| --- | --- | --- |
| `guardrail.no_secret_material_in_a_turn` | input | A check: declared expressions, refused and redacted in `governance/guardrails.py` |
| `guardrail.declared_operations_only` | planning | A check: the plan validator in `planner/planner.py` |
| `guardrail.identifiers_are_never_invented` | planning | An instruction, in the planning prompt |
| `guardrail.exact_payload_approval` | execution | A check: the payload hash in `governance/approval.py` |
| `guardrail.read_back_before_confirmation` | execution | A check: the read-back in `governance/verification.py` |
| `guardrail.reply_states_verified_outcomes_only` | output | An instruction, in the reply prompt |

Three of the six — the plan validator, the approval gate, the verifier — are
code that exists whether or not the catalog names it, and naming it is what
makes the promise countable. The fourth, the input check, is built from the
expressions the catalog declares and exists because of it. The remaining two
are only ever a sentence, and the document says so rather than letting them sit
unmarked among the others.

### The publish-time gate

The validator checks every identifier a skill declares against the identifiers
the configured catalog defines, and an identifier the catalog does not define
is `guardrail_unknown_id`. A deployment that configures no catalog, or names
one that is not on disk, defines none — so declaring any is then an error,
while a catalog that defines none still validates. The same rule runs over a
proposed procedure, so a promise nothing keeps cannot be published.

`guardrails.enabled: false` hands the engine an empty catalog. Nothing is
checked, and nothing is stated either: a deployment that has switched its
guardrails off does not tell a model it keeps promises it is not keeping.
Validation is a separate question and reads the document whatever that flag
says, so a catalog that still validates is not a catalog whose promises are
still kept.

## Policy files

A policy sits next to the skill that declares it, and the path in `policy:` is
resolved against the skill file. `schemas/policy.schema.json` is the schema.

```json
{
  "skill_id": "runbook.alert_silence",
  "default": "deny",
  "rules": [
    {
      "operation": "alert.silence",
      "effect": "deny",
      "when": { "minutes": { "gt": 240 } },
      "reason": "A silence longer than four hours outlives the work it was set for."
    },
    {
      "operation": "alert.silence",
      "effect": "allow",
      "when": { "minutes": { "lte": 240 } },
      "max_per_session": 4,
      "reason": "A bounded silence while the cause is worked on."
    }
  ]
}
```

`skill_id` must name the skill the file belongs to. `default` is `deny` and
cannot be anything else. Rules are evaluated in order and the first match wins,
so the specific case goes above the general one.

**`operation`** is an operation name or `*` for every write of the procedure.

**`effect`** is `allow`, `deny` or `require_second_approval`.

**`when`** is a map of dotted path to a map of operator to value. Every
condition must hold for the rule to match. The subject is the payload merged
with the entity records read earlier in the plan, keyed by the short entity name
— so `service.environment` reads the `environment` field of the service record
the plan read, and `minutes` reads the payload field. A path that is not there
does not match, which means a rule that depends on a read that did not happen
simply does not apply.

Operators: `eq`, `ne`, `in`, `not_in`, `lt`, `lte`, `gt`, `gte`. Values of
different shapes never satisfy an ordering condition; they compare false rather
than raising.

**`max_per_session`** applies to a rule that would otherwise allow. The ledger
is asked how many receipts this session holds for this operation, in any state,
and once the count reaches the limit the decision becomes `deny` with a reason
that names the limit. Attempts are what the limit counts, so a write that
failed has already consumed one of them.

**`reason`** is the sentence the user is given when the rule decides. It is
written for a person, because it is what they read.

A rule may name an operation the procedure lists under `reads`, and the
validator allows it, but reads never reach the policy engine: `evaluate`
short-circuits to `allow` for anything the catalog does not declare as a write.

## Approval

`ApprovalGate.request` copies the payload, hashes the canonical JSON rendering
of it with SHA-256, and keeps both on the pending approval along with the plan,
the index of the operation inside it, the integration and the operation name.
The canonical rendering sorts keys and uses compact separators, so equal content
always hashes the same and different content never does.

`check` re-hashes a payload and compares it with the frozen hash. Two payloads
go through it before the write runs: the one the held plan carries for the
approved operation, and the frozen copy. Both are stored with the session, so
either can move while the approval waits, and checking the frozen copy alone
would only compare it with itself. A difference on either side records a
`rejected` outcome with the reason `approval payload no longer matches the
plan`, emits `approval_decided` carrying that reason, clears the approval and
tells the user to ask again. Nothing runs.

The hash is what makes the text the user reads binding. The approval request
prints `service.restart on integration.container_runtime with
{"service":"search"}`, and that string is the canonical rendering of the payload
the hash covers.

A second approval is a second decision on the same frozen payload. The first
`yes` increments `approvals_given`, emits `approval_decided` with both counts,
records another `approval_pending` outcome and re-states the request with
`1 of 2 approvals recorded`. Only when `approvals_given` reaches
`approvals_required` does the write proceed. The gate does not know who gave
either approval; identity is the channel's problem, discussed below.

Three things cancel a pending approval: an explicit `no` (a `rejected`
outcome), an explicit cancel (a `cancelled` outcome), and a new request. The
last one matters: a new task cancels the approval waiting for an answer, with
the reason `the approval waiting for an answer was cancelled by the new
request`. A goal is replaced, never merged.

## Verification

The verification spec is resolved from the operation that was executed.
`spec.verify.operation` names a read operation, and `CapabilityRegistry` looks
for it on the same integration first, falling back to a scan of published
integrations for that operation name.

The payload for the read-back is the write payload narrowed to the properties
the read operation's input schema declares. Nothing is invented: if the read
takes `service` and the write carried `service` and `replicas`, the read gets
`service` alone.

Each entry of `verify.expect` is a dotted path into the read-back response and
an expected value, either a literal or `$payload.<field>` resolved from the
write payload. Comparison is by value with a string fallback. The evidence
recorded on the receipt holds the read operation, the payload sent, whether it
succeeded, the data it returned, and one comparison entry per expectation with
`expected`, `actual`, `found` and `match`.

The entity the operation is bound to adds to that set. When it declares a state
field and the closed set of states the field may hold, a transition declared on
the operation supplies the expected value for that field, unless `expect`
already names the field, in which case the author's expectation stands. And a
read-back that returns a value outside the declared states fails verification
whatever the other comparisons said, with `state_outside_declared_set` on the
evidence naming the field, the value read and the set it left. When the write
was a move of a declared lifecycle, a read-back that lands on a state other
than the one the move promised is recorded as `state_the_move_promised`, with
what was expected and what was read.

Three statuses:

- `verified` — the read-back succeeded and every expectation matched.
- `failed` — the read-back ran and contradicted the write, or returned an error.
- `unverified` — the write itself did not report success, no connector is bound,
  or the read-back could not be run at all.

`unverified` is not a pass. The reply says the write ran and nothing confirmed
it, and the receipt carries the reason.

## The ledger

SQLite, at `state/ledger.sqlite` by default, one table of receipts; PostgreSQL
behind the `storage.backend` setting for more than one process sharing a
database rather than a file. Both satisfy the same `LedgerStore` protocol in
`oska.storage`, exercised by one contract test suite — see
[docs/configuration.md#storage](configuration.md#storage). Every call opens
and closes its own connection, so a SQLite ledger survives a restart and can
be read by another process while the runtime is up.

The **ledger key** is `sha256(canonical_json([integration_id, operation,
subject]))`, where `subject` is the payload narrowed to the declared
`idempotency_fields`, sorted, or the whole payload when none are declared. It is
what "the same write" means. Lookup is scoped to the session: the same write in
another session is another intent, because the ledger exists to stop one
conversation from doing the same thing twice. It is a per-session record, and a
deployment that needs a lock across sessions has to build one.

A **receipt** holds the receipt id, the session, the ledger key, the integration
and operation, the payload, the response, the status (`pending`, `executed`,
`error`), the failure classification, the catalog revision the turn was decided
under, the verification status, the verification evidence, the creation time
and the error text. `oska receipts --session <id>` prints them oldest first,
`--json` in full.

The receipt is written before the connector is called. That is deliberate, and
it is what makes an **uncertain write** safe. If the process dies between the
call and the response, the receipt stays `pending` — and a later identical
request finds it and stops, reporting the receipt rather than sending the write
again. An error or an unverified write blocks a retry just as a verified one
does. Retrying is a decision for a person, made by asking for something the key
does not cover.

The one failure that does not block is the one that never arrived. A receipt
whose failure the connector classified `retryable` is left in the record and
passed over by `settled`, so the same intent may be asked for again in the same
session. That is not a weakening of the rule; it is the rule stated exactly. The
ledger stops one conversation from doing the same thing twice, and a call that
did not reach the system did not do it once.

A write whose request left but whose answer was `ambiguous` is not treated as
either case by assumption: its read-back decides whether it landed, whether it
did not, or whether the runtime has to leave the key closed because it cannot
tell — see
[When the system does not answer](#when-the-system-does-not-answer) for what
each of those three does to the receipt.

## Audit

Every step emits a typed event. The sink appends one JSON object per line to
`state/audit.jsonl`; each event carries its kind, the session, a turn id that is
unique per turn, the time, and a data object. The turn also returns its own
events, which is what the conformance runner inspects.

| Kind | Raised when |
| --- | --- |
| `turn` | A turn opens. Carries the turn number and the text |
| `route` | The router decided. Kind, procedure named, confidence, which layer answered, language |
| `discovery` | The catalog was ranked. The ranking, the choice, whether it was a tie |
| `hydrate` | A procedure was resolved to its closure. Entities, integrations, step and step count |
| `plan` | A plan was built or refused. Operations, missing inputs, reply intent, or the issues |
| `policy` | A policy decided. Skill, operation, effect, reason, and `stage: approval` on re-evaluation |
| `approval_requested` | A write was frozen. Approval id, operation, payload, approvals required, effect |
| `approval_decided` | Approved, rejected or cancelled, with the counts or the reason |
| `execute` | Twice per write: `stage: intent` with the receipt and ledger key, `stage: call` with the result |
| `verify` | A read-back ran. Operation, receipt, status, evidence |
| `ledger_hit` | A write was already done in this session. Operation, receipt, key, verification |
| `reply` | The turn answered. The reply kind and what is pending |
| `guardrail` | A guardrail acted. The point, the identifier, whether it blocked or redacted, and why |
| `budget` | A turn asked for a model call past its budget. The limit, what was spent, how many were refused |
| `model_fallback` | A model call of the turn moved from one profile to the next. Which profile answered each call, and each fallback with its role, the profile left, the one tried next and the reason: an HTTP status or the class of the failure, never the content |
| `unavailable` | A call did not reach the system. Integration, operation, whether it was a write, the error, the breaker state and when a call may go out again |
| `revision` | The session changed the catalog revision it reads through. Which revision it moved from, which to, and why |
| `retention` | A session was removed from the record. The operation, and how many receipts, events and stored states went |
| `trace` | Tracing is on for this turn. Where the spans are being written |
| `proposal` | Reserved for the evolution layer |

Nothing downstream of a sink can change what the runtime does: `emit` returns
nothing and the runtime never reads back what it wrote.

Traces are a separate thing and are not a substitute for any of this. A span
says how long a stretch of a turn took and what it was nested in; it is
optional, off by default, and a deployment may throw its traces away without
losing anything it has to keep. Nothing in the runtime reads a trace back and
no decision is taken from one. When a question can only be answered from a
trace, the answer is to raise an audit event, not to leave tracing on.

## Identity

`identity.mode` in `configs/runtime.json` is `proxy` or `token`. Either way,
identity now governs two things: a turn's principal, described in the two
modes below, and who may reach which route and be recorded as whose actor,
described in [Who may do what over HTTP](#who-may-do-what-over-http) after
them.

### Proxy mode

The default. The HTTP application has no authentication of its own; it assumes
an authenticated proxy in front of it that decides who may reach it. The
boundary is drawn here on purpose: the runtime records who a caller said they
were, and the channel is responsible for making that claim true. A deployment
that exposes the API without such a proxy has no identity on its approvals or
its lifecycle transitions, whatever the audit trail says.

A turn may carry a principal: the subject of whoever is signed in, and whatever
else about them the deployment puts on the call. Over HTTP it arrives in
`X-Principal-Subject` and `X-Principal-Attributes`, which an authenticated
proxy sets. The runtime holds no key, checks no signature and knows no
directory, so a deployment that lets anything but its proxy reach that port has
handed every session whatever identity the caller typed. There is no
corresponding option on the command line, because there is no proxy there to
believe.

### Token mode

`identity.mode: token` moves that verification into the runtime itself. A
caller sends `Authorization: Bearer <token>`, and the runtime checks the
signature before it reads a single claim out of it — `governance/identity.py`,
`verify_token`. The algorithm is fixed at HS256; a token declaring `alg: none`
or anything else is refused outright, the same as a bad signature, not treated
as a lesser form of trust. The key is read from the environment variable
`identity.key_env` names, `OSKA_SESSION_TOKEN_KEY` by default, and a missing
key is a `500 identity_key_missing` at request time rather than a silent fall
back to trusting the caller.

`identity.required_claims` (`sub`, `sid`, `exp` by default) must all be
present; `exp` and, when carried, `nbf` are checked against the clock within
`identity.leeway_seconds`; `identity.tenant_claim` (`tenant` by default) is
exposed as an attribute of the principal the same way any other claim is, and
is what a platform reads to resolve which tenant a call is for — see
[docs/packs-and-platforms.md#tenant](packs-and-platforms.md#tenant).

One check is specific to the call being made rather than to the token itself:
a call that acts inside a session compares the token's `sid` claim against
that session's id, and a call that acts in no session — drafting a proposal,
listing domains — skips the comparison entirely rather than requiring a `sid`
that names nothing. Every other claim check that fails is a `401
invalid_token` naming the check; the `sid` check alone comes back as a `403
session_refused`, worded the same as any other refusal of a session, because a
token issued for another session is a session problem, not a malformed token:

```
POST /sessions/demo-1/turns   Authorization: Bearer <token, sid=demo-1>   → 200
POST /sessions/other/turns    Authorization: Bearer <token, sid=demo-1>   → 403 session_refused
                                 "this call may not act in this session"
```

The runtime never mints a token it later accepts. Whoever authenticates the
person issues it; `oska.governance.identity.mint_token` exists for tests and
for an operator's own tooling, not as something the runtime calls on its own
behalf. [docs/building-a-platform.md](building-a-platform.md#8-move-from-proxy-identity-to-token-identity)
walks moving a deployment from proxy to token mode end to end.

One check is made, and it is about consistency rather than authenticity. A
session opened without an identity stays anonymous: an identity arriving once
the session holds anything of an earlier turn — a history, a goal waiting on
a question, a write waiting on an approval — is refused the same `403
session_refused`, so nobody takes over a conversation or answers an approval
somebody else was asked. A session that holds an identity is that person's: a
turn arriving under a different subject, or under none at all, is refused the
same way, and nothing of it is run or recorded. A session is one conversation
with one person, and a write bound to an identity halfway through would be
bound to whichever identity happened to be on that turn.

When it comes to filling a field, a principal the channel marked as not
authenticated counts as no principal at all.

### Who may do what over HTTP

`identity.required` refuses, with `401 identity_required`, any call that
carries no identity — except `GET /health`, which stays open whatever this
says, and `GET /metrics`, which is read like the reviewer routes below are:
by a principal carrying the reviewer role, or by any caller at all where
`metrics.public` says so (`docs/configuration.md#metrics`). `identity.required`
defaults to `false` in `proxy` mode; in `token` mode it defaults to `true`,
because a deployment that verifies tokens and still answers a call without
one has verified nothing. Either default is overridden by setting the key
explicitly.

Five routes read or move the review queue, and each requires a principal
carrying `identity.reviewer_role` (`"reviewer"` by default); a principal
without it is refused with `403 reviewer_required`, naming only the missing
role:

- `GET /proposals`, `GET /proposals/{proposal_id}` — read proposals.
- `POST /proposals/{proposal_id}/transition` — move one.
- `GET /changes`, `GET /changes/{request_id}` — read change requests.

Drafting from a session, `POST /proposals/from-session/{session_id}` (and its
per-domain form), is not one of the five: it stays open to the session's own
principal and to a reviewer. Anyone else is refused: a non-reviewer caller
that carries an identity is refused drafting from a session opened by another
identity, or from one opened without one at all, with the same
`403 session_refused` a turn on that session would get. A session with no
history has nothing to draft from and is answered `404` the same whether or
not the session exists, so a caller cannot tell the two apart. `GET /domains`
is open to every caller, but names each domain's tenants only to a caller
that may review; anyone else sees the domain and its host with an empty
tenant list.

A call passes the reviewer check either way it can be met: by carrying the
reviewer role, or by carrying no identity at all in a deployment whose
`identity.single_user` is `true` — the one person who reaches such a
deployment without an identity does a reviewer's work as its one user. A call
with no identity in any other deployment is refused there, `401
identity_required` when none is required either, `403 reviewer_required`
otherwise: no identity is never worth more than an identity that lacks the
role. `single_user` and `token` mode never coexist — the configuration itself
refuses to load that combination — so a deployment that verifies tokens is
never reached this way.

The actor recorded on a proposal transition, and on the change request it
composes, is the caller's principal when the call carries one —
`ActorSource.principal`. Only when it carries none, and the deployment
declares `identity.single_user`, does a name given in the call's body count,
and it is recorded as `ActorSource.stated` and marked unverified on the
transition's history and on the change request. A transition run from the
command line is always `ActorSource.operator`, and an operator counts as
verified: the terminal running the command is the authority, the same way an
authenticated proxy is over HTTP. `oska change publish` refuses to author a
git commit under a stated, unverified actor unless
`--allow-unverified-actor` says the operator means it.

### Fields taken from it

A procedure that acts on a person's own account should not ask which account
that is. `principal_inputs` maps payload field to identity attribute, field by
field:

```yaml
principal_inputs:
  username: subject
```

Those fields are never asked for and never offered to the model. They are
filled from the identity before the plan runs, into every operation whose input
schema accepts them, so the policy judges the value that will be sent. Three
refusals follow from that:

- A plan that supplies one of these fields itself is refused, naming the field
  and the operation. It is not quietly corrected: a value the model chose and a
  value the channel authenticated are not interchangeable, and the difference is
  why the field was declared.
- A procedure that declares one and is reached by a session with no
  authenticated identity is refused, saying so. The value is not the person's
  to give, so there is no question to ask.
- The validator refuses a field no operation of the procedure accepts, and one
  that is also declared a human input. A field is either asked of the person or
  taken from the identity.

The whole plan is read before any of it runs, so a read is not executed on the
strength of a plan whose write names an identity the person chose.

## Retention

A conversation is personal data. The account this system keeps of one is
spread over eight stores — the receipts of what it wrote, the audit trail of
how it decided, the state it carried between turns, the judgments recorded on
its turns, what was learned about how it reads, the trace spans that name it,
the proposals drafted from it, and the change requests composed from those
proposals — and a window or a request to be forgotten has to reach all eight
or it has reached none of them. `governance/retention.py` is the only thing in
the system that takes anything out of the record.

```sh
oska forget session walk
oska forget expired --days 90
```

`forget` answers a request. It runs whatever the configured window is and
whether or not the session is past it, because a person asking is not a
schedule. `expire` applies `retention.days`, measured from the moment each
session was last written, which is when the conversation it holds last
happened. A window of zero days, which is the default, keeps everything:
nothing is removed and nothing is recorded as having been.

Removal leaves a line behind. The retention event names the session, the
operation and the counts, and nothing else: not what was said, not what was
read, not what was written. It survives the removal it records, because a
deletion nobody can see having happened is indistinguishable from a file
somebody edited. The audit trail is rewritten once for all the sessions named
rather than once per session, and the events are written into the trail that
replaced it.

A proposal drafted from the session, and a change request composed from it,
are removed the same way — except an applied change request, whose decision
record survives: who asked, who decided, when, which version, and where it
landed, with the drafted material itself cleared. A decision once made is
kept; what it was made from is not. Both are counted alongside the other six
stores, in the retention report and in the retention event.

Neither command runs inside the serving process. A window is an operator's job
on an operator's schedule, and a request to be forgotten is a decision somebody
takes and can say they took. Both are destructive and neither is reversible.

The window is not the whole answer and is not meant to be. What must never be
stored is kept out of the record in the first place — that is what the input
guardrail at the top of a turn is for. What is stored because the account needs
it is kept for as long as the window says and no longer.
