---
id: integration.${system}
type: integration
version: 1
status: published
description: Reads and archives ${entities} held by the system of record this pack integrates with.
entities:
  - entity.${entity}
operations:
  - name: ${entity}.get
    write: false
    entity: entity.${entity}
    input_schema:
      type: object
      additionalProperties: false
      required: [name]
      properties:
        name:
          type: string
          description: ${Entity} identifier.
    output_schema:
      type: object
      properties:
        ok:
          type: boolean
        ${entity}:
          type: object
  - name: ${entity}.archive
    write: true
    entity: entity.${entity}
    transitions:
      status: archived
    input_schema:
      type: object
      additionalProperties: false
      required: [name]
      properties:
        name:
          type: string
          description: ${Entity} identifier.
    output_schema:
      type: object
      properties:
        ok:
          type: boolean
        ${entity}:
          type: object
    verify:
      operation: ${entity}.get
      expect:
        ${entity}.status: archived
        ${entity}.name: $payload.name
    idempotency_fields: [name]
---

## Purpose

The one binding this pack needs to prove itself: a read and a governed write,
each declared here and mapped to the mock connector in
`configs/connectors.json`. Point `configs/connectors.json` at a real system
when this pack is ready to run against one, without changing anything here.

## Notes

The write declares its own read-back in `verify`: `${entity}.archive` is
reported done only once `${entity}.get` shows `status: archived` under the
same name. This is what `guardrail.read_back_before_confirmation` holds a
procedure to; do not remove it when adding a write of your own.
