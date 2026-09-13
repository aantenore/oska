# Adopting opsdeck on an existing system

The runtime owns no business data. It reads and writes through the integration
layer and keeps only conversation state, receipts, audit and proposals. Putting
it on top of something you already run is a matter of declaring what is
there, not of moving anything.

Work through five steps, in order. Each is testable on its own and useful
before the next is done. The fragments below are drawn from the reference
domain under `examples/ops/`: services, deployments, incidents and alerts.

## 1. Declare the ontology

Start with the records people already talk about: one entity skill per record
type. Fields, one of them the identifier, the state field and its values if
the record has a lifecycle, and the relations to other records.

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
    identifier: true
    description: Stable service name, unique within the estate.
  - name: status
    type: string
    description: "Current lifecycle state: running, degraded or stopped."
state_field: status
states: [running, degraded, stopped]
relations:
  - name: deployments
    target: entity.deployment
    cardinality: many
    description: Deployments recorded against this service.
---
```

Declare states only when the system really has a closed set: the list is what
a write's `transitions` is checked against, and a wrong one surfaces as a
validation error later. Name fields the way your operators say them, not the
way the database does — this vocabulary is what the model and the people
share.

Check it with `opsdeck validate`, and look at it with
`opsdeck ontology --format mermaid`.

## 2. Declare the integrations, with verification

One integration skill per system holds the closed list of what can be done
there: a name, a read or write flag, the entity it acts on, an input schema
and an output schema. Nothing outside that list can be planned, approved or
executed.

The part that takes thought is the write. It must declare `verify.operation`,
a read of the same integration; `verify.expect`, dotted paths in that read's
response checked against literals or `$payload.<field>`; `idempotency_fields`,
the payload fields that decide when two requests are the same request; and
`transitions`, when the write leaves the entity in a known state.

```yaml
  - name: service.restart
    write: true
    entity: entity.service
    transitions:
      status: running
    input_schema:
      type: object
      required: [service]
      properties:
        service: { type: string }
    verify:
      operation: service.status
      expect:
        service.name: $payload.service
        service.status: running
    idempotency_fields: [service]
```

If you cannot name a read that proves a write worked, that is worth knowing
before anything automates it.

Bind the integration in the connectors file. Start with `mock` over a fixture
of real-looking records, so the catalog and scenario suite come together
before anything touches a real system; move to `http` or `shell` later by
editing the binding alone, since the operation names and payload fields are
declared by the skill.

## 3. Write the first procedure, read-only

Pick the question people ask most often and answer it without changing
anything. A read-only runbook needs no policy, so this step exercises the
catalog, the discovery, the routing and the planning without touching the
write path.

```yaml
---
id: runbook.service_health
type: runbook
version: 1
status: published
description: Reports the current state of a service and the tail of its log, without changing anything.
triggers:
  - how is the checkout service doing
  - is the api healthy
  - check the status of a service
entities:
  - entity.service
integrations:
  - integration.container_runtime
capabilities:
  reads:
    - service.status
    - service.logs
runtime:
  action: check the health of a service
  action_on_selection: check its health
  order: 1
---
```

Write the body for a person on call: purpose, preconditions, steps,
verification, rollback. It is not decoration — it goes into the planning
prompt, and a `Steps` section naming the operations in order is what makes a
plan hold up.

Run `opsdeck capabilities`, then `opsdeck run --text "..."`. The scope
sentence you get back is computed from the `runtime` block; if it reads
badly, fix the wording there, not a prompt.

## 4. Add the first governed write, with a policy

Add a runbook that reads first and then writes, list the write under
`capabilities.writes`, and put a `policy.json` next to it.

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

Three things to get right. Order rules from specific to general, since the
first match wins. Condition on what the plan has read: the subject of a
`when` is the payload merged with the entity records already read, keyed by
the short entity name, so `service.environment` works because the health
check ran first. Set `max_per_session` on everything that allows.

Run it and read the approval request: the payload shown is the payload that
runs. Answer `yes`, and the engine re-reads the state, re-evaluates the
policy, records the intent, calls the connector, reads the service back, and
only then calls the receipt verified.

## 5. Write the scenarios

Turn what you just did by hand into files under a scenario directory and run
`opsdeck conform --scenarios <dir>`. At minimum: the read-only procedure,
level 1; the governed write with its approval, verified receipt and a
repeated request that hits the ledger, level 2; the policy denial; a
verification failure, forced by making the write succeed and the read-back
disagree; and a rejected or cancelled approval.

The format, and the rules for keeping a script honest, are in
[conformance.md](conformance.md). From here the suite says whether a change
broke something, and it runs offline in seconds.

## The standalone variant

Nothing above requires an enterprise system underneath. If the connectors are
the system — argv over local tooling, one `shell` binding per utility — the
model is unchanged and so are the invariants.

```json
"integration.deployment_control": {
  "type": "shell",
  "operations": {
    "deployment.rollback": {
      "argv": ["deployctl", "rollback", "--service", "{service}",
               "--to", "{target_version}", "--output", "json"],
      "timeout": 120
    }
  }
}
```

The integration skill still declares `deployment.rollback` as a write, with a
`verify` pointing at a read and an expectation about the resulting version.
The argument vector is rendered one field at a time and never handed to a
shell, so a payload value can only ever become one argument. The policy, the
approval, the ledger and the read-back work exactly as they do overlaid on an
enterprise system. What changes is what you own: local tools are the state,
and `state/` is still the only thing the runtime itself writes.

## Where approvals and policies live in an enterprise deployment

The runtime does not decide where a `yes` comes from. The channel carrying
the turn can carry the approval too, adequate when it is authenticated and
the writes are low-consequence, or the request can be filed as a change
ticket with the decision returned as a later turn. Either way the payload
hash travels with the request, so what comes back approved is what was
frozen, and the policy is re-evaluated against current state first.

Policies stay files next to the procedures they govern, versioned with the
skills and reviewed the same way. An organization that centralizes policy
decisions can put that service behind the interface the engine already
calls: a decision on an operation and a payload, back as an effect and a
reason.

The HTTP application itself has no authentication; it assumes an
authenticated proxy decides who may reach it, and the actor recorded on an
approval or a proposal transition is exactly what the proxy said it was. See
[governance.md](governance.md#identity).
