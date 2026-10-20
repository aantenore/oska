---
id: runbook.${entity}_archive
type: runbook
version: 1
status: published
description: Archives ${a_entity} after confirming its current status, and verifies the read-back.
triggers:
  - archive ${entity} demo-1
  - retire ${entity} demo-1, it is done
  - this ${entity} is done, archive it
entities:
  - entity.${entity}
integrations:
  - integration.${system}
capabilities:
  reads:
    - ${entity}.get
  writes:
    - ${entity}.archive
guardrails:
  - guardrail.declared_operations_only
  - guardrail.identifiers_are_never_invented
  - guardrail.exact_payload_approval
  - guardrail.read_back_before_confirmation
runtime:
  action: archive ${a_entity}
  action_on_selection: archive it
  order: 2
policy: policy.json
---

## Purpose

The governed half of this pack: a write that only ever runs at the payload a
person approved, and is only ever reported done once the read-back agrees.

## Preconditions

- The ${entity} name is known and it is currently `active`.

## Steps

1. Read `${entity}.get` and confirm the current status.
2. Call `${entity}.archive` with the exact name.
3. Read `${entity}.get` again and confirm the status is `archived`.

## Verification

The read-back must show status `archived` under the same name. A response to
the write is not evidence on its own: the read-back is. If the read-back shows
anything else, report the archive as unverified and do not repeat it.

## Rollback

This pack declares no operation that reverses an archive. A real integration
that can reopen a record should add one and a matching lifecycle move; until
then, treat an archive as a decision made on purpose.
