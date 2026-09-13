---
id: integration.alerting
type: integration
version: 1
status: published
description: Lists the alerts currently firing for a service and silences one of them for a set time.
entities:
  - entity.service
  - entity.alert
operations:
  - name: alert.list
    write: false
    entity: entity.alert
    input_schema:
      type: object
      additionalProperties: false
      properties:
        service:
          type: string
          description: Restrict the list to one service.
        alert_id:
          type: string
          description: Restrict the list to one alert.
        state:
          type: string
          enum: [firing, silenced, resolved]
          description: Restrict the list to one state.
    output_schema:
      type: object
      properties:
        ok:
          type: boolean
        alerts:
          type: array
  - name: alert.silence
    write: true
    entity: entity.alert
    transitions:
      state: silenced
    input_schema:
      type: object
      additionalProperties: false
      required: [alert_id, minutes]
      properties:
        alert_id:
          type: string
          description: Alert to silence.
        minutes:
          type: integer
          minimum: 1
          maximum: 1440
          description: How long the silence lasts.
    output_schema:
      type: object
      properties:
        ok:
          type: boolean
        alert:
          type: object
    verify:
      operation: alert.list
      expect:
        alerts.0.id: $payload.alert_id
        alerts.0.state: silenced
    idempotency_fields: [alert_id]
---

## Purpose

The paging surface. It says what is firing and lets one alert be quietened for
a bounded time while the cause is dealt with.

## Notes

A silence is a promise to come back. Set the shortest silence that covers the
work in flight, and open an incident first so the silence has a written reason.

Silencing hides the symptom from the on-call rotation, not from the customer.
