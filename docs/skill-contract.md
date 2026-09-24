# The skill contract

A skill is one Markdown file with YAML frontmatter. The frontmatter is the
declaration the runtime enforces. The body is the procedure as a person would
write it, and it reaches the model as part of the planning prompt.

## Where files live

```
<catalog root>/<type>/<name>/v<version>/skill.md
<catalog root>/<type>/<name>/v<version>/policy.json      (when the skill writes)
<catalog root>/<type>/<name>/v<version>/evaluation.json  (the requests it must answer)
```

The registry discovers `**/v*/skill.md` under every configured root. An id is
`<type>.<snake_name>`, matching `^(runbook|entity|integration|composite)\.[a-z0-9_]+$`,
and the type in the id must match the declared type. Several versions of the
same id may sit side by side: the effective one is the highest published
version, or the highest version of all when none is published.

## Common frontmatter

| Key | Applies to | Meaning |
| --- | --- | --- |
| `id` | all | `<type>.<name>` |
| `type` | all | `entity`, `integration`, `runbook` or `composite` |
| `version` | all | Integer, 1 or more |
| `status` | all | `draft`, `published` or `retired`. Defaults to `published` |
| `description` | all | 20 to 200 characters. Discovery ranks against it |
| `triggers` | runbook, composite | What a person says to ask for this. At least three, distinct. Forbidden elsewhere |
| `entities` | runbook, integration | Entity ids this skill touches |
| `integrations` | runbook | Integration ids this runbook reaches |
| `capabilities` | runbook, composite | `reads` and `writes`, by operation name |
| `runtime` | runbook, composite | `action`, `action_on_selection`, `order` |
| `policy` | runbook | Path to the policy file, relative to the skill file |
| `operations` | integration | The declared operations |
| `fields`, `state_field`, `states`, `relations` | entity | The ontology |
| `initial_state`, `final_states`, `lifecycle`, `state_read` | entity | The state machine, when the record has one |
| `human_inputs` | runbook, composite | Payload fields only a person may supply |
| `principal_inputs` | runbook, composite | Payload fields taken from the authenticated identity, as field to attribute |
| `guardrails` | all | Identifiers of the promises this skill keeps |
| `runbooks` | composite | The member runbooks, in order |
| `subject` | runbook, composite | The entity a follow-up in this procedure refers to, and whether it joins the conversation's focus |
| `presentation` | runbook, composite | What to show at a named moment of the turn, bound to this turn's own evidence |
| `noun`, `plural` | entity | How one record and several are named in a reply |
| `display_fields` | entity | Which fields a person may refer to a record by, other than its identifier |
| `comparable_fields` | entity | Which fields a comparative question may refine on |

The body is split on H2 headings. `purpose`, `preconditions`, `steps`,
`verification` and `rollback` are read into named sections; any other heading is
kept under `extra`.

## Entity

An entity says what a record is. It declares no operations and no triggers:
nothing routes to an entity, it is referenced by the procedures that touch it.

```yaml
---
id: entity.service
type: entity
version: 1
status: published
description: A running service, its environment, the version it serves and how many replicas are up.
fields:
  - name: name
    type: string
    identifier: true            # exactly the field an operation matches on
    description: Stable service name, unique within the estate.
  - name: status
    type: string
    description: "Current lifecycle state: running, degraded or stopped."
  - name: restarts
    type: integer
    description: Restart count since the last deployment.
state_field: status             # must be one of the declared fields
states:                         # the closed set a transition may name
  - running
  - degraded
  - stopped
relations:
  - name: deployments
    target: entity.deployment   # must be a published entity in the catalog
    cardinality: many           # one or many
    description: Deployments recorded against this service, most recent first.
---

## Purpose

The unit every other record points at. Identify it by `name`; the same name
exists once per environment, so carry `environment` whenever an operation can
touch production.
```

`state_field` and `states` go together: one without the other is an error. The
states are what the ontology check on an integration's `transitions` is made
against.

### The lifecycle

A record whose states are a machine rather than a label declares that machine.
Four more keys, all of them optional together and none of them alone:

```yaml
state_field: state
states: [in_progress, succeeded, failed, rolled_back]
initial_state: in_progress      # must be one of the declared states
final_states:                   # states nothing moves out of
  - rolled_back
state_read: deployment.get      # a read bound to this entity; where the state comes from
lifecycle:
  - from: [in_progress, succeeded, failed]   # declared states, any number
    to: rolled_back                          # a declared state
    operation: deployment.rollback           # a write bound to this entity
    required: false             # the move that completes the object. Defaults to false
    offered: true               # whether the runtime may suggest it. Defaults to true
```

`state_read` is what makes the lifecycle enforceable: the runtime reads the
current state through it, from the system of record, and never takes the state
from the conversation. Declaring a lifecycle without it is an error.

`required` marks the move that completes the object — until it is made, the
object is not yet what the person asked for — and at most one move may be
required out of any one state. `offered: false` marks a move that is legal but
must never be suggested: the runtime plans and runs it when a person asks for
it, and never proposes it. Neither field affects legality, only what is said.

A state that only ever appears under `from` is a state the system of record
puts the object in — `succeeded` and `failed` above are decided by a release
going out, not by anything in this catalog. A state that appears under `to` and
cannot be reached from `initial_state` is an error, because nothing here could
ever put the object there.

What the runtime does with all this is in
[governance.md](governance.md#the-lifecycle-of-an-object).

### Naming and comparison

```yaml
noun: alert
plural: alerts
display_fields: [id, service, rule, state]
comparable_fields: [state, service]
```

All four are legal only on an entity; a runbook or a composite declaring any
of them is an error. `noun` defaults to the entity's short id segment
(`entity.alert` → `alert`) and `plural` to `noun` with `s` appended when not
given — there is no irregular-plural support, so an entity whose plural is not
its noun plus `s` declares `plural` itself. `display_fields` are the fields a
person may refer to a record by other than its identifier — "the one in
finance" — and `comparable_fields` are the fields a comparative question may
refine on, when the records on screen actually differ on them. Both are read
by the subject engine rather than validated against anything beyond being
fields the entity declares; the semantics are in
[presentation.md](presentation.md#subject-what-the-conversation-is-about).

## Integration

An integration is the closed list of what can be done. Every operation carries
its schemas, and every write carries its read-back.

```yaml
---
id: integration.container_runtime
type: integration
version: 1
status: published
description: Reads the state and logs of a running service and changes how many instances of it run.
entities:
  - entity.service               # every operation's entity must be listed here
operations:
  - name: service.status
    write: false
    entity: entity.service
    input_schema:
      type: object
      additionalProperties: false
      required: [service]
      properties:
        service:
          type: string
          description: Service name.
    output_schema:
      type: object
      properties:
        ok: { type: boolean }
        service: { type: object }

  - name: service.restart
    write: true
    entity: entity.service
    transitions:
      status: running            # the state this write leaves the entity in
    input_schema:
      type: object
      additionalProperties: false
      required: [service]
      properties:
        service:
          type: string
          description: Service name.
    output_schema:
      type: object
      properties:
        ok: { type: boolean }
        service: { type: object }
    verify:
      operation: service.status  # must be a read operation of this integration
      expect:
        service.name: $payload.service   # dotted path in the read response
        service.status: running          # literal
    idempotency_fields: [service]        # must be properties of the input schema
---

## Purpose

The runtime that holds the processes.

## Notes

`service.restart` is not a fix. Read the status after every write: the runtime
accepts the request before the process is up, so the response to the write says
nothing about the outcome.
```

Three things to note.

`verify` is not optional on a write. The model refuses to build an operation
that sets `write: true` without one, so a write with no declared read-back
cannot be loaded at all.

`expect` compares a dotted path in the read-back response against either a
literal or `$payload.<field>`, taken from the write payload. Comparison is by
value, falling back to string rendering, so the literal `"3"` matches the number
`3`. A missing path is a failure, not an absence.

`idempotency_fields` decide what "the same write" means. With `[service]`, two
restarts of `search` collide even if the rest of the payload differs. With no
fields declared, the whole payload is the key.

## Runbook

A runbook is what a person asks for. The frontmatter says what it may reach; the
body says how it is done and reaches the planner as part of its prompt.

```yaml
---
id: runbook.service_restart
type: runbook
version: 1
status: published
description: Restarts a service after checking its current state, and confirms it came back up.
triggers:                        # at least three, distinct
  - restart the checkout service
  - bounce the api
  - can you restart payments
  - riavvia il servizio
entities:
  - entity.service
integrations:
  - integration.container_runtime
capabilities:
  reads:
    - service.status             # must be a read declared by a listed integration
  writes:
    - service.restart            # must be a write declared by a listed integration
guardrails:                      # must be defined by the configured guardrail catalog
  - guardrail.declared_operations_only
  - guardrail.identifiers_are_never_invented
  - guardrail.exact_payload_approval
  - guardrail.read_back_before_confirmation
runtime:
  action: restart a service      # how it is named in a list of what the runtime can do
  action_on_selection: restart it # how it is named once a record is already on the table
  order: 3                       # where it appears; unordered procedures come last
policy: policy.json              # required, because this runbook writes
---

## Purpose

Clear a process that is stuck. A restart buys time. It does not fix a bad
version.

## Preconditions

- The service name and its environment are known.
- The current status has been read.

## Steps

1. Read `service.status` and note the version, the replica count and the
   restart count.
2. Call `service.restart` with the exact service name.
3. Read `service.status` again and compare against what was expected.

## Verification

The read-back must show the service `running` under the same name. A response
to the write is not evidence.

## Rollback

A restart cannot be undone. If the service comes back worse, roll the
deployment back.
```

The `runtime` block is the only place the runtime's wording for a procedure
comes from. `oska capabilities` prints it; the HTTP `/capabilities` route
returns it; the reply writer is given it as the list of things it may suggest.

A runbook with an empty `writes` list needs no policy: reads are gated by
declaration alone. A runbook that writes and declares no policy is a validation
error, and at run time a missing policy denies.

`guardrails` names the promises this procedure keeps, by identifier. The
wording of each lives in the guardrail catalog and nowhere else, so several
procedures claiming the same promise cannot claim it differently, and an
identifier the catalog does not define fails validation. Entities and
integrations may declare them too: a procedure is held to what its whole
closure declares. The catalog format, and which promises are code and which are
text, are in [governance.md](governance.md#guardrails).

### Fields only a person may supply

`human_inputs` names payload fields whose value has to come from the person,
not from the model.

```yaml
human_inputs:
  - note
```

The planner is told which fields these are and that it may not fill them in.
If it does anyway, the plan is rejected with an issue naming the field, and the
repair is to leave the field out of the payload and put it in `missing` with
the question to ask. The turn then stops and asks, the answer is stored against
that field, and the next plan carries the value the person gave — compared
exactly, so a value that drifted between the question and the payload is caught
the same way an invented one is.

This is for fields where being approximately right is worse than being absent:
the text of an incident note, a justification, a reason recorded against a
change. A composite's members each declare their own, and a goal is held to the
union.

### Fields taken from the identity

`principal_inputs` names payload fields whose value comes from the identity the
channel authenticated, mapping each field to the attribute it is read from.

```yaml
principal_inputs:
  username: subject
```

`subject` is what that identity is called; any other attribute the deployment
put on the call is named the same way. A procedure that acts on a person's own
account declares the account field here and then never asks which account it
is: the field is not offered to the model, is filled before the plan runs, and
a plan that supplies it anyway is refused rather than corrected. A session with
no authenticated identity does not reach such a procedure at all — the refusal
says so, because the value is not the person's to give.

Two rules the validator enforces. Every declared field must be one that some
operation reachable from this procedure accepts, or the value would be resolved
on every turn and written into no payload. And a field cannot be both a human
input and a principal input: it is either asked of the person or taken from the
identity. What the write path does with all this is in
[governance.md](governance.md#identity).

### Subject and presentation

```yaml
subject:
  entity: entity.service
  cardinality: one
  joins: focus
presentation:
  on_selection:
    view: service_selected
    bind:
      service: $selection
  on_verified_write:
    view: service_restarted
    bind:
      service: $receipt.service
      receipt: $receipt.receipt_id
      verification: $receipt.verification
```

Both are legal only on a runbook or a composite; an entity declaring either is
an error. `subject.entity` names what a follow-up in this procedure refers to;
`cardinality` is `one` or `many`; `joins: focus` means this procedure accepts
the record the conversation's focus already resolved to as a collected input,
rather than only what the person names this turn. `presentation` is one entry
per named moment — `on_verified_write`, `on_selection`, `on_results`,
`on_single_result`, `on_completion` — each a `view` name your own client
switches on and a `bind` map of field to a binding expression resolved against
this turn's evidence alone. Neither field is validated beyond its own shape by
the schema; the deeper checks — a binding whose field does not exist, a
`joins: focus` procedure whose entity has no `subject_field` an operation
accepts — are the `presentation_*` and `subject_*` codes below, raised by
`oska.ontology.naming` and run as part of `oska validate`. What either
declaration does at runtime, the binding grammar, and the precedence between
moments are in [presentation.md](presentation.md).

### The evaluation set beside it

A ranking threshold that is a hand-set number is a guess, and a guess ages. What
does not age is a worked example. A skill may carry an `evaluation.json` in its
version directory holding the requests that must reach it and the requests that
must not:

```json
{
  "cases": [
    { "text": "please restart the payments service", "expects": "this", "language": "en" },
    { "text": "riavvia il servizio di pagamento", "expects": "this", "language": "it" },
    { "text": "undo the last deployment of the api", "expects": "not this", "language": "en" }
  ]
}
```

`expects` is `this` or `not this` and defaults to `this`. `language` is stated
rather than guessed, and an empty value means the author did not say.

```sh
oska evaluate catalog
oska evaluate suggest runbook.service_health
```

`evaluate catalog` runs every set in the catalog through the same ranking the
runtime uses, and leaves a non-zero status on any failing case, so a new
procedure that takes another's requests fails the build instead of a
conversation. It ranks by keyword overlap and needs no provider, which is what
makes it identical on every machine; a deployment that ranks by embedding should
run the same sets against its own provider before publishing.

The cases that matter are the ones that nearly match — a set written from a
skill's own triggers proves very little, since those words reach it by
construction. `evaluate suggest` drafts that half, drawn from the declared
triggers of the skills that rank closest, and prints it to paste and edit. It
writes nothing, because which neighboring requests genuinely compete is
a judgment about the catalog.

A set is optional. `validate` does not require one, and the quality gate refuses
a proposal whose set is there and failing rather than one whose set is absent:
making it mandatory would push authors to paste in cases that pass rather than
cases that matter. Both commands name the procedures carrying no set, so the
absence is visible rather than silent.

### The policy beside it

```json
{
  "skill_id": "runbook.service_restart",
  "default": "deny",
  "rules": [
    {
      "operation": "service.restart",
      "effect": "allow",
      "when": { "service.environment": { "ne": "production" } },
      "max_per_session": 2,
      "reason": "Outside production a restart is routine and reversible by redeploying."
    },
    {
      "operation": "service.restart",
      "effect": "require_second_approval",
      "when": { "service.environment": { "eq": "production" } },
      "max_per_session": 2,
      "reason": "A production restart drops in-flight requests and is visible to customers."
    }
  ]
}
```

The format is in [governance.md](governance.md).

## Composite

A composite is a fixed sequence of runbooks. It declares triggers and runtime
wording like a runbook, and nothing about operations: its capabilities are the
union of its members', filled in by the loader when they are left out, and each
member still runs under its own policy.

```yaml
---
id: composite.incident_triage
type: composite
version: 1
status: published
description: "Triages a service under an open incident: reads its health, searches its logs, then records what was found."
triggers:
  - triage the incident on checkout
  - investigate the incident and write it up
  - fai il triage dell incidente
runbooks:                        # run in this order
  - runbook.service_health
  - runbook.log_search
  - runbook.incident_note
runtime:
  action: triage an incident on a service
  action_on_selection: triage it
  order: 7
---

## Purpose

The first ten minutes of a response, in a fixed order.

## Steps

1. Run `runbook.service_health` for the service.
2. Run `runbook.log_search` with a pattern from the incident summary.
3. Run `runbook.incident_note` with one line carrying status, version and the
   most telling log line.

Each step runs under its own policy. The note is a write and stops for approval
on its own terms, even though the two steps before it did not.
```

A composite declares no policy of its own; declaring one is an error. It is
excluded from discovery ranking by `discovery.exclude_types` in
`configs/runtime.json`, so it is reached by its triggers on the fast path or by
the model naming it, not by a semantic score competing with its own members.

## Validator rules

`oska validate` runs `registry.parse_errors()` and then every rule below over
every skill. Each issue prints as `severity: skill_id: code: message`, and the
command exits non-zero when there is at least one.

| Code | Applies to | Raised when |
| --- | --- | --- |
| `parse_error` | any file | The file cannot be read, is not UTF-8, has no frontmatter, or the frontmatter fails the typed model |
| `schema` | all | The frontmatter does not satisfy `schemas/skill.schema.json` |
| `unknown_reference` | all | A referenced entity, integration or runbook is not in the catalog |
| `wrong_reference_type` | all | A reference resolves to a skill of another type |
| `unpublished_reference` | all | A reference resolves to a skill that is not published |
| `capability_mismatch` | composite | Declared capabilities are not the union of the members' |
| `undeclared_capability` | runbook | A capability names an operation no listed integration declares |
| `read_is_write` | runbook | An operation under `reads` is declared as a write |
| `write_is_read` | runbook | An operation under `writes` is not declared as a write |
| `missing_verify` | integration | A write operation declares no read-back |
| `unknown_verify_operation` | integration | The `verify.operation` is not a read operation of this integration |
| `unknown_idempotency_field` | integration | An idempotency field is not a property of the input schema |
| `ontology_unknown_state_field` | entity | `state_field` is not one of the declared fields |
| `ontology_missing_states` | entity | A state field with no states, or states with no state field |
| `ontology_unknown_relation_target` | entity | A relation targets something that is not an entity in the catalog |
| `ontology_unpublished_relation_target` | entity | A relation targets an entity that is not published |
| `ontology_unlisted_operation_entity` | integration | An operation binds to an entity the integration does not list |
| `ontology_unbound_operation` | integration | An operation of a published integration declares no `entity` |
| `ontology_transition_without_entity` | integration | An operation declares transitions but no entity in the catalog |
| `ontology_unknown_transition_field` | integration | A transition names a field that is not the entity's state field |
| `ontology_unknown_transition_state` | integration | A transition names a value that is not a declared state |
| `ontology_lifecycle_without_states` | entity | A lifecycle is declared without a state field and its states |
| `ontology_lifecycle_without_initial_state` | entity | A lifecycle is declared without the state an object starts in |
| `ontology_unknown_lifecycle_state` | entity | The initial state, a final state, or a state named by a move is not one of the declared states |
| `ontology_unknown_lifecycle_operation` | entity | A move names an operation that is not bound to this entity, or one that does not write |
| `ontology_missing_state_read` | entity | A lifecycle is declared without `state_read` |
| `ontology_unknown_state_read` | entity | `state_read` is not an operation bound to this entity, or is a write |
| `ontology_move_from_final_state` | entity | A move leaves a state declared final |
| `ontology_unreachable_state` | entity | A move leads to a state that cannot be reached from the initial state |
| `ontology_conflicting_required_move` | entity | One state declares more than one required move |
| `principal_input_not_declared` | runbook, composite | A field taken from the identity is not a field any operation of this procedure accepts |
| `principal_input_shadows_human_input` | runbook, composite | A field is declared both a human input and a principal input |
| `presentation_unknown_display_field` | entity | `display_fields` names a field the entity does not declare |
| `presentation_unknown_comparable_field` | entity | `comparable_fields` names a field the entity does not declare |
| `subject_unknown_entity` | runbook, composite | `subject.entity` is not an entity in the catalog |
| `subject_unlisted_entity` | runbook | `subject.entity` is not one of the entities the runbook lists |
| `subject_unjoinable` | runbook, composite | `subject.joins: focus`, and no operation of this procedure accepts a field naming a record of that entity |
| `presentation_unknown_evidence` | runbook, composite | A binding names a kind other than `items`, `item`, `selection` or `receipt` |
| `presentation_malformed_binding` | runbook, composite | A binding that takes no field names one, or a binding that needs one names none |
| `presentation_unknown_field` | runbook, composite | An `items`/`item` binding's field is not a field of the subject's entity or of an entity this procedure reads |
| `presentation_unverified_input` | runbook, composite | A binding is `$inputs.<field>` — what a person typed, which is not evidence until a verified write carries it |
| `presentation_receipt_without_write` | runbook, composite | A `receipt` binding is declared and this procedure writes nothing. Printed with severity `warning`, the only code that is — `oska validate` still exits non-zero when it appears, the same as any other issue |
| `presentation_unknown_receipt_field` | runbook, composite | A `receipt` binding's field is neither a standard receipt field nor a field of a payload this procedure writes |
| `missing_runtime` | runbook, composite | No `runtime` block |
| `too_few_triggers` | runbook, composite | Fewer than three triggers |
| `duplicate_triggers` | runbook, composite | The same trigger twice |
| `unexpected_policy` | composite | A composite declares a policy |
| `missing_policy` | runbook | The procedure writes and declares no policy |
| `missing_policy_file` | runbook | The declared policy file is not there |
| `invalid_policy_file` | runbook | The policy file cannot be read or is not JSON |
| `policy_schema` | runbook | The policy does not satisfy `schemas/policy.schema.json` |
| `invalid_policy` | runbook | The policy does not satisfy the typed model |
| `policy_skill_mismatch` | runbook | `skill_id` in the policy names another skill |
| `policy_undeclared_operation` | runbook | A rule names an operation that is not a capability of the procedure |
| `guardrail_unknown_id` | all | A declared guardrail is not defined by the configured guardrail catalog |

The guardrail rule reads the catalog named by `guardrails.catalog` in the runtime
configuration, looked for in the working directory and then beside that file. A
deployment that configures no catalog, or names one that is not there, defines no
identifier: declaring any is then an error, and a catalog that declares none still
validates. The same rule runs over a proposed procedure, so a promise nothing keeps
cannot be published.

Some constraints never become an issue code because the typed model refuses the
file outright, and the registry reports it as `parse_error`: an id that does not
match its type, a description outside 20 to 200 characters, triggers on an
entity or an integration, operations on anything but an integration, fields,
states, relations or any of the lifecycle keys on anything but an entity,
`human_inputs` or `principal_inputs` on anything but a runbook or a composite,
the same field named by both, `runbooks` on anything but a composite, a
composite with no runbooks, and a write operation with no `verify`.

An evaluation set is not part of validation. It is a file beside the skill
rather than a declaration in it, and `oska evaluate catalog` is what reads
it; a set that cannot be read is reported there and by the quality gate, not by
`validate`.
