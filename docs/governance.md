# Governance

Every change to the world goes through the same sequence. The sequence is in
the code, not in configuration: there is no setting that removes a step, and a
procedure with no policy writes nothing.

## The write path

A planned operation whose declared spec says `write: true` stops the plan. What
follows happens in `engine/execution.py` and `governance/`.

1. **Declared.** The operation must be declared by the integration the plan
   names. `CapabilityRegistry.operation_spec` looks it up; an operation that is
   not there ends the plan with an `invalid_plan` outcome. This check already
   ran for every operation of the plan at the planner, and runs again here,
   because the plan is data and data is checked where it is used.
2. **Allowed by the skill.** The operation must appear in
   `capabilities.writes` of the procedure being run. A plan that reaches for a
   write the procedure does not list is an `invalid_plan` outcome, even though
   the integration declares it.
3. **Ledger lookup.** The ledger key is computed and looked up for this
   session. A hit emits `ledger_hit`, records a `ledger_hit` outcome with the
   original receipt, and moves on to the next operation. The write does not run
   twice.
4. **Policy.** `PolicyEngine.evaluate` loads the policy the procedure declares
   and applies the first matching rule to the payload merged with the entity
   records the plan has read so far. The effect is emitted as a `policy` event.
   `deny` ends the plan with a `denied` outcome carrying the reason.
5. **Approval.** `ApprovalGate.request` freezes the operation on the exact
   payload, hashes it, and stores a `PendingApproval` on the session, with
   `approvals_required` set to 2 when the policy said
   `require_second_approval` and 1 otherwise. An `approval_requested` event
   carries the approval id, the operation, the payload and the number required.
   The turn ends with an `approval_pending` outcome and a reply that shows the
   payload verbatim. Nothing has run.
6. **Intent recorded.** When the approval arrives and the hash still matches,
   steps 1 and 2 are run again over the catalog as it stands now, because the
   catalog is files on disk and the approval was given in an earlier turn. An
   operation that has since been withdrawn, turned into a read, or dropped from
   the procedure's writes ends the turn with an `invalid_plan` outcome and
   nothing in the ledger. The policy is then evaluated again — against the
   entities as they are now, rather than as they were when the question was
   asked — and a receipt is written to the ledger with status `pending`, before
   the call goes out. An `execute` event with `"stage": "intent"` carries the
   receipt id and the ledger key.
7. **Execute.** The connector is called with the operation name and the payload.
   A connector that raises is caught, and the failure becomes an outcome. An
   `execute` event with `"stage": "call"` records whether it succeeded and how
   long it took. The receipt is updated to `executed` or `error` with whatever
   the call returned.
8. **Verify.** The read-back the integration declares is run and its result
   compared against `verify.expect`. A `verify` event carries the status and the
   evidence.
9. **Ledger update.** The verification status and its evidence are written to
   the receipt. The receipt is now the record of what happened.
10. **Reply.** The reply is built from the outcomes of the turn. When a model
    phrases it, the deterministic line of every receipt and every ledger hit is
    appended to what the model wrote, so the account of the write comes from
    the receipt even when the prose around it does not. A write that was not
    verified is reported as not verified, and the conformance runner fails a
    scenario whose replies never name a receipt, or name one as verified while
    the receipt says otherwise.

If a composite still has steps after the approved write, the plan continues from
the operation after it and the goal advances, so one approval unblocks the rest
of the sequence without unblocking any other write.

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
evidence naming the field, the value read and the set it left.

Three statuses:

- `verified` — the read-back succeeded and every expectation matched.
- `failed` — the read-back ran and contradicted the write, or returned an error.
- `unverified` — the write itself did not report success, no connector is bound,
  or the read-back could not be run at all.

`unverified` is not a pass. The reply says the write ran and nothing confirmed
it, and the receipt carries the reason.

## The ledger

SQLite, at `state/ledger.sqlite` by default, one table of receipts. Every call
opens and closes its own connection, so the file survives a restart and can be
read by another process while the runtime is up.

The **ledger key** is `sha256(canonical_json([integration_id, operation,
subject]))`, where `subject` is the payload narrowed to the declared
`idempotency_fields`, sorted, or the whole payload when none are declared. It is
what "the same write" means. Lookup is scoped to the session: the same write in
another session is another intent, because the ledger exists to stop one
conversation from doing the same thing twice. It is a per-session record, and a
deployment that needs a lock across sessions has to build one.

A **receipt** holds the receipt id, the session, the ledger key, the integration
and operation, the payload, the response, the status (`pending`, `executed`,
`error`), the verification status, the verification evidence, the creation time
and the error text. `opsdeck receipts --session <id>` prints them oldest first,
`--json` in full.

The receipt is written before the connector is called. That is deliberate, and
it is what makes an **uncertain write** safe. If the process dies between the
call and the response, or the call times out, the receipt stays `pending` — and
a later identical request finds it and stops, reporting the receipt rather than
sending the write again. `lookup` returns the most recent receipt for the key
whatever its outcome, so an error or an unverified write blocks a retry just as
a verified one does. Retrying is a decision for a person, made by asking for
something the key does not cover.

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
| `proposal` | Reserved for the evolution layer |

Nothing downstream of a sink can change what the runtime does: `emit` returns
nothing and the runtime never reads back what it wrote.

## Identity

The HTTP application has no authentication. It assumes an authenticated proxy in
front of it that decides who may reach it. The `by` field on a proposal
transition is recorded exactly as it arrives and is not verified against
anything. The same is true of `--by` on the command line.

The boundary is drawn here on purpose. The runtime records who a caller said
they were, and the channel is responsible for making that claim true. A
deployment that exposes the API without such a proxy has no identity on its
approvals or its lifecycle transitions, whatever the audit trail says.
