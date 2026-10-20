---
id: runbook.${entity}_status
type: runbook
version: 1
status: published
description: Reports the current status of ${a_entity} by reading it back from the system of record.
triggers:
  - what is the status of ${entity} demo-1
  - check ${entity} demo-1
  - is ${entity} demo-1 still active
entities:
  - entity.${entity}
integrations:
  - integration.${system}
capabilities:
  reads:
    - ${entity}.get
guardrails:
  - guardrail.identifiers_are_never_invented
runtime:
  action: check ${a_entity}'s status
  action_on_selection: check its status
  order: 1
---

## Purpose

The read-only half of this pack: confirm what ${a_entity} is doing right now,
with nothing planned beyond the read.

## Preconditions

- The ${entity} name is known; it is never guessed.

## Steps

1. Read `${entity}.get` with the exact name given.

## Verification

The reply states the status exactly as read back, and nothing this procedure
did not read.
