---
id: integration.request_tracker
type: integration
version: 1
status: published
description: Reads the access requests people raised and records the outcome on one of them once it is settled.
entities:
  - entity.access_request
operations:
  - name: request.get
    write: false
    entity: entity.access_request
    input_schema:
      type: object
      additionalProperties: false
      required: [id]
      properties:
        id:
          type: string
          description: Request identifier.
    output_schema:
      type: object
      properties:
        ok:
          type: boolean
        access_request:
          type: object
  - name: request.list
    write: false
    entity: entity.access_request
    input_schema:
      type: object
      additionalProperties: false
      properties:
        user:
          type: string
          description: Restrict the list to the requests about one account.
        state:
          type: string
          enum: [open, granted, revoked, denied]
          description: Restrict the list to one state.
    output_schema:
      type: object
      properties:
        ok:
          type: boolean
        access_requests:
          type: array
  - name: request.close
    write: true
    entity: entity.access_request
    input_schema:
      type: object
      additionalProperties: false
      required: [id, state]
      properties:
        id:
          type: string
          description: Request to close.
        state:
          type: string
          enum: [granted, revoked]
          description: Outcome to record, matching the change that was made.
    output_schema:
      type: object
      properties:
        ok:
          type: boolean
        access_request:
          type: object
    verify:
      operation: request.get
      expect:
        access_request.id: $payload.id
        access_request.state: $payload.state
    idempotency_fields: [id]
---

## Purpose

Where the ask is recorded and where the answer goes back. A request is opened by
the person who wants the access; this integration reads what is open and closes
one of them with the outcome that actually happened.

## Notes

`request.close` declares no transition, because the state it leaves the request
in is not fixed: it is whichever of `granted` or `revoked` the payload carries,
and the read-back is what says the two agree. An expectation on
`access_request.state` against `$payload.state` says exactly that and claims
nothing more.

The two outcomes this operation records are the two that follow a change of
membership. A request a reviewer turns down is settled in the tracker by the
reviewer, and nothing here writes `denied` on their behalf.
