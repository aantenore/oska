---
id: integration.container_runtime
type: integration
version: 1
status: published
description: Reads the state and logs of a running service and changes how many instances of it run.
entities:
  - entity.service
operations:
  - name: service.status
    write: false
    entity: entity.service
    input_schema:
      type: object
      additionalProperties: false
      required: [service]
      properties:
        service:
          type: string
          description: Service name.
    output_schema:
      type: object
      properties:
        ok:
          type: boolean
        service:
          type: object
  - name: service.logs
    write: false
    entity: entity.service
    input_schema:
      type: object
      additionalProperties: false
      required: [service]
      properties:
        service:
          type: string
          description: Service name.
        lines:
          type: integer
          minimum: 1
          maximum: 500
          description: How many of the most recent lines to return.
        pattern:
          type: string
          description: Case-insensitive substring; only matching lines are returned.
    output_schema:
      type: object
      properties:
        ok:
          type: boolean
        lines:
          type: array
  - name: service.restart
    write: true
    entity: entity.service
    transitions:
      status: running
    input_schema:
      type: object
      additionalProperties: false
      required: [service]
      properties:
        service:
          type: string
          description: Service name.
    output_schema:
      type: object
      properties:
        ok:
          type: boolean
        service:
          type: object
    verify:
      operation: service.status
      expect:
        service.name: $payload.service
        service.status: running
    idempotency_fields: [service]
  - name: service.scale
    write: true
    entity: entity.service
    input_schema:
      type: object
      additionalProperties: false
      required: [service, replicas]
      properties:
        service:
          type: string
          description: Service name.
        replicas:
          type: integer
          minimum: 0
          maximum: 20
          description: Replica count to run.
    output_schema:
      type: object
      properties:
        ok:
          type: boolean
        service:
          type: object
    verify:
      operation: service.status
      expect:
        service.name: $payload.service
        service.replicas: $payload.replicas
    idempotency_fields: [service, replicas]
---

## Purpose

The runtime that holds the processes. It answers what a service is doing right
now, hands back its recent output, and is the only place a restart or a change
of replica count happens.

## Notes

`service.restart` is not a fix. It clears a stuck process and buys time; if the
cause is a bad version, the service comes back and fails again. Read the status
after every write: the runtime accepts the request before the process is up, so
the response to the write says nothing about the outcome.

`service.logs` returns at most the most recent lines it holds. A pattern is a
plain substring match, not a regular expression.
