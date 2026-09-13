---
id: runbook.service_health
type: runbook
version: 1
status: published
description: Reports the current state of a service and the tail of its log, without changing anything.
triggers:
  - how is the checkout service doing
  - is the api healthy
  - check the status of a service
  - show me the health of payments
  - come sta il servizio
  - stato del servizio
entities:
  - entity.service
integrations:
  - integration.container_runtime
capabilities:
  reads:
    - service.status
    - service.logs
runtime:
  action: check the health of a service
  action_on_selection: check its health
  order: 1
---

## Purpose

Answer the first question of any response: is this service up, on which
version, and what is it saying. Read-only, so it is safe to run at any point
and costs nothing but a moment.

## Preconditions

- The service name is known. If the same name exists in more than one
  environment, the environment is known too.

## Steps

1. Read `service.status` for the named service.
2. Read `service.logs` for the same service, most recent 50 lines.
3. Report status, version, replicas and restart count, then the lines that
   carry an error or warning. Quote the log, do not summarise it away.

## Verification

None to perform: nothing was changed. The status read is itself the evidence,
and it is reported with the version it was read at.

## Rollback

Not applicable.
