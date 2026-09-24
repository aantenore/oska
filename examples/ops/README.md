# Example pack: running services

A small estate of six services, three in staging and three in production, with
their deployments, incidents and alerts. It exists so the runtime has something
concrete to run against. Nothing here is part of the package: the core knows
about skills, operations, policies and receipts, and learns this domain only by
reading these files.

This directory is a pack: `pack.yaml` names it, what it `provides` and the
runtime versions it `requires`, checked against these files by
`oska pack check .`. See
[docs/packs-and-platforms.md](../../docs/packs-and-platforms.md) for the pack
contract, and `examples/platform.yaml` for this pack served as the
`operations` domain of a platform alongside `examples/access/`.

## What is in it

- `pack.yaml` the manifest: name, version, what this pack provides, the host
  identity it documents for a platform to use, the conformance level it
  claims.
- `skills/` the catalog, one directory per skill and one directory per version.
- `fixtures/` the records the mock connector reads and writes. Six services and
  their recent log lines, eight deployments, three incidents, four alerts.
- `configs/connectors.json` binds each integration to the mock connector.
- `configs/connectors.real.example.json` the same catalog bound to an HTTP API
  and a command line tool, as it would look against real systems.
- `scenarios/` the conformance suite for this catalog, one session per file.
- `evaluation/` the labeled routing set the improvement job is measured
  against, and the redaction patterns it filters a learned phrase through.
- `cassettes/` where a recorded session against a real provider is kept.

## How the skills map to the model

Four kinds of skill, each answering a different question.

**Entities** say what a record is. `entity.service`, `entity.deployment`,
`entity.incident` and `entity.alert` declare their fields and which field
identifies them. They declare no operations and no triggers: nothing routes to
an entity, it is referenced by the procedures that touch it.

**Integrations** say what can be done, exactly. `integration.container_runtime`,
`integration.deployment_control`, `integration.alerting` and
`integration.incident_tracker` each declare a closed list of operations with an
input schema, an output schema, and for every write a read-back and the payload
fields that form its idempotency key. An operation that is not declared here
cannot be planned, approved or executed.

**Runbooks** are the procedures a person asks for. Each one declares its
triggers, the entities it works on, the integrations it reaches, and the
operations it is allowed to use, split into reads and writes. The markdown body
is the procedure itself, written the way it would be written for a person on
call: purpose, preconditions, steps, verification, rollback. A runbook that
writes carries a `policy.json` next to it; a read-only runbook does not need
one, because reads are gated by declaration alone.

Three of them — `runbook.service_restart`, `runbook.alert_silence` and
`runbook.log_search` — also carry an `evaluation.json`, holding the requests
that must reach them and the requests that must not. `oska evaluate catalog`
runs all of them and names the procedures carrying none, so a new runbook that
starts taking another's requests fails there rather than in a conversation.

**Composites** are fixed sequences of runbooks. `composite.incident_triage`
runs the health check, then the log search, then the incident note. Each step
keeps its own policy, so the note still stops for approval even though the two
reads before it did not. A composite declares no policy of its own and its
capabilities are the union of its runbooks'.

The wording the runtime uses comes from the `runtime` block of each runbook:
`action` names the procedure in a list, `action_on_selection` names it once a
record is already on the table, `order` fixes where it appears.

## The mock binding

`configs/connectors.json` maps each declared operation onto a fixture. Paths in
it resolve against the file itself. The keys of an operation mapping:

- `collection` the array in the fixture to work on.
- `match` / `payload_field` the record field to match and the payload field
  that carries the value.
- `where` extra equality conditions the record must meet, such as the
  deployment that is current.
- `filter` payload fields that narrow a list, with `field_map` when the payload
  name and the record name differ.
- `project` return one field of the matched record instead of the record.
- `as` the key the result is returned under.
- `limit_field` / `limit_default` how many items to return.
- `contains` substring filter over a list field, taken from a payload field.
- `set` fields to write: a literal, `$field` to copy a payload field, or `+1`
  to increment.
- `append` list fields to append a payload field to.

Writes land in an in-memory copy of the fixture, so a session changes state and
the files on disk stay as they are.
