---
id: entity.service
type: entity
version: 1
status: published
description: A running service, its environment, the version it serves and how many replicas are up.
fields:
  - name: name
    type: string
    identifier: true
    description: Stable service name, unique within the estate.
  - name: environment
    type: string
    description: Environment the instance runs in, staging or production.
  - name: status
    type: string
    description: "Current lifecycle state: running, degraded or stopped."
  - name: version
    type: string
    description: Version tag currently served.
  - name: replicas
    type: integer
    description: Number of replicas expected to be up.
  - name: restarts
    type: integer
    description: Restart count since the last deployment.
state_field: status
states:
  - running
  - degraded
  - stopped
relations:
  - name: deployments
    target: entity.deployment
    cardinality: many
    description: Deployments recorded against this service, most recent first.
  - name: incidents
    target: entity.incident
    cardinality: many
    description: Incidents raised against this service.
  - name: alerts
    target: entity.alert
    cardinality: many
    description: Alerts whose rule watches this service.
---

## Purpose

The unit every other record points at. A deployment moves a service from one
version to another, an incident is opened against a service, an alert fires for
a service. Identify it by `name`; the same name exists once per environment, so
carry `environment` whenever an operation can touch production.

## Notes

`restarts` is a cheap signal of instability. A service that has restarted more
than a handful of times since its last deployment is usually crash-looping, and
restarting it again buys nothing.
