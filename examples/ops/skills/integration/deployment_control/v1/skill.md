---
id: integration.deployment_control
type: integration
version: 1
status: published
description: Lists what was deployed to a service and puts an earlier version back in place.
entities:
  - entity.service
  - entity.deployment
operations:
  - name: deployment.get
    write: false
    entity: entity.deployment
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
        deployment:
          type: object
  - name: deployment.list
    write: false
    entity: entity.deployment
    input_schema:
      type: object
      additionalProperties: false
      properties:
        service:
          type: string
          description: Restrict the list to one service.
        limit:
          type: integer
          minimum: 1
          maximum: 50
          description: How many records to return, most recent first.
    output_schema:
      type: object
      properties:
        ok:
          type: boolean
        deployments:
          type: array
  - name: deployment.rollback
    write: true
    entity: entity.deployment
    transitions:
      state: rolled_back
    input_schema:
      type: object
      additionalProperties: false
      required: [service, target_version]
      properties:
        service:
          type: string
          description: Service name.
        target_version:
          type: string
          description: Version to put back in place.
    output_schema:
      type: object
      properties:
        ok:
          type: boolean
        deployment:
          type: object
    verify:
      operation: deployment.get
      expect:
        deployment.service: $payload.service
        deployment.version: $payload.target_version
        deployment.state: rolled_back
    idempotency_fields: [service, target_version]
---

## Purpose

The record of what changed and the lever that undoes it. A rollback here is a
forward move to a known-good version, recorded as its own deployment.

## Notes

A rollback that names no version is a guess. Read the current deployment first
and take `previous_version` from it; a version that was never deployed to this
service will be accepted by the call and fail on the way up.

Rolling back does not undo data changes made by the version being removed.
