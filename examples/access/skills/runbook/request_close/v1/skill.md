---
id: runbook.request_close
type: runbook
version: 1
status: published
description: Records on an access request the change that was actually made, and reads the request back.
triggers:
  - close the access request
  - mark the request as granted
  - the ticket can be settled now
  - record the outcome on the request
  - chiudi la richiesta di accesso
  - registra l esito sulla richiesta
entities:
  - entity.access_request
integrations:
  - integration.request_tracker
capabilities:
  reads:
    - request.get
  writes:
    - request.close
guardrails:
  - guardrail.declared_operations_only
  - guardrail.identifiers_are_never_invented
  - guardrail.exact_payload_approval
  - guardrail.read_back_before_confirmation
runtime:
  action: record the outcome of an access request
  action_on_selection: record its outcome
  order: 4
policy: policy.json
---

## Purpose

Close the loop. The request is what an access review reads a year later, so it
has to say what happened, not what was intended. This runs after the membership
has changed and never before it.

## Preconditions

- The request identifier is known.
- The membership change it records has already been made and read back. A
  request closed as granted while the account is still outside the group is
  worse than one left open.
- The request is still open. One that is already settled is left alone.

## Steps

1. Read `request.get` and check the request is open, and that the account and
   group it names are the ones that were changed. The state comes from that
   read: closing a request a colleague settled in the meantime is refused on
   what the tracker holds, not on what was said here.
2. Call `request.close` with the identifier and the outcome: granted when the
   membership was added, revoked when it was taken away.
3. Read the request back and confirm it carries that outcome.

## Verification

The read-back must show the request under the same identifier with the state
that was sent. Nothing else is claimed: closing a request is a statement about
the record, not a second confirmation that the access changed.

## Rollback

A request cannot be reopened here. If it was closed with the wrong outcome or
against the wrong identifier, say so to whoever owns the tracker and raise a
fresh request for the access that is actually wanted.
