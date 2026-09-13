# The skill contract

A skill is one Markdown file with YAML frontmatter. The frontmatter is the
declaration the runtime enforces. The body is the procedure as a person would
write it, and it reaches the model as part of the planning prompt.

## Where files live

```
<catalog root>/<type>/<name>/v<version>/skill.md
<catalog root>/<type>/<name>/v<version>/policy.json      (when the skill writes)
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
| `runbooks` | composite | The member runbooks, in order |

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
comes from. `opsdeck capabilities` prints it; the HTTP `/capabilities` route
returns it; the reply writer is given it as the list of things it may suggest.

A runbook with an empty `writes` list needs no policy: reads are gated by
declaration alone. A runbook that writes and declares no policy is a validation
error, and at run time a missing policy denies.

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

`opsdeck validate` runs `registry.parse_errors()` and then every rule below over
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

Some constraints never become an issue code because the typed model refuses the
file outright, and the registry reports it as `parse_error`: an id that does not
match its type, a description outside 20 to 200 characters, triggers on an
entity or an integration, operations on anything but an integration, fields or
states on anything but an entity, `runbooks` on anything but a composite, a
composite with no runbooks, and a write operation with no `verify`.
