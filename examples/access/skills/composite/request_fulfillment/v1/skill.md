---
id: composite.request_fulfillment
type: composite
version: 1
status: published
description: "Settles an open access request: reads what the account already has, grants the membership, records the outcome."
triggers:
  - fulfil this access request
  - process the open request for the new joiner
  - handle the request and close the ticket
  - grant what the request asks for and settle it
  - evadi questa richiesta di accesso
  - gestisci la richiesta e chiudila
runbooks:
  - runbook.access_check
  - runbook.access_grant
  - runbook.request_close
guardrails:
  - guardrail.declared_operations_only
  - guardrail.identifiers_are_never_invented
  - guardrail.exact_payload_approval
  - guardrail.read_back_before_confirmation
runtime:
  action: settle an open access request end to end
  action_on_selection: settle it
  order: 5
---

## Purpose

The whole of a routine request, in the order that keeps the record true: find
out what the account already has, change the membership, then write down what
was done. Reversing the last two steps is how a tracker ends up full of
requests marked granted against access nobody ever got.

## Preconditions

- The request is open and its identifier is known.
- The request names an account and a group, and both exist.

## Steps

1. Run `runbook.access_check` for the account the request names. Note the
   groups it already holds, so a request for one it already has is answered
   rather than granted twice.
2. Run `runbook.access_grant` for the account and the group the request names.
3. Run `runbook.request_close` with the request identifier and the outcome
   `granted`.

Each step runs under its own policy. The grant stops for approval on its own
terms, and a privileged group asks for a second one, even though the read
before it did not stop for anything. Closing the request is a write too, and it
stops for its own approval after the membership has been read back.

## Verification

The first step changes nothing and is its own evidence. The grant is verified by
reading the membership back, and closing the request by reading the request
back. The request is settled only when both read-backs hold; if the grant was
not confirmed, the request stays open and says so.

## Rollback

Nothing here is undone as a sequence. Revoke the membership with
`runbook.access_revoke` if it should not have been granted, and raise a fresh
request for whatever access is actually wanted.
