---
id: integration.incident_tracker
type: integration
version: 1
status: published
description: Reads incidents raised against a service and appends notes to the timeline of one of them.
entities:
  - entity.service
  - entity.incident
operations:
  - name: incident.get
    write: false
    entity: entity.incident
    input_schema:
      type: object
      additionalProperties: false
      required: [incident_id]
      properties:
        incident_id:
          type: string
          description: Incident identifier.
    output_schema:
      type: object
      properties:
        ok:
          type: boolean
        incident:
          type: object
  - name: incident.list
    write: false
    entity: entity.incident
    input_schema:
      type: object
      additionalProperties: false
      properties:
        service:
          type: string
          description: Restrict the list to one service.
        state:
          type: string
          enum: [open, mitigated, closed]
          description: Restrict the list to one state.
    output_schema:
      type: object
      properties:
        ok:
          type: boolean
        incidents:
          type: array
  - name: incident.note
    write: true
    entity: entity.incident
    input_schema:
      type: object
      additionalProperties: false
      required: [incident_id, note]
      properties:
        incident_id:
          type: string
          description: Incident to append to.
        note:
          type: string
          minLength: 3
          maxLength: 2000
          description: One line describing what was observed or done.
    output_schema:
      type: object
      properties:
        ok:
          type: boolean
        incident:
          type: object
    verify:
      operation: incident.get
      expect:
        incident.id: $payload.incident_id
        incident.last_note: $payload.note
    idempotency_fields: [incident_id, note]
---

## Purpose

The timeline. Every observation, every action taken and every decision made
during a response is written here while it is still true.

## Notes

Notes are append-only and the same note text is written once per incident: a
repeated note is a duplicate, not a second event. Write what was observed and
what was done, not what is assumed.
