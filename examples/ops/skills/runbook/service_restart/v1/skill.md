---
id: runbook.service_restart
type: runbook
version: 1
status: published
description: Restarts a service after checking its current state, and confirms it came back up.
triggers:
  - restart the checkout service
  - bounce the api
  - can you restart payments
  - the service is stuck, restart it
  - riavvia il servizio
  - fai ripartire il servizio
entities:
  - entity.service
integrations:
  - integration.container_runtime
capabilities:
  reads:
    - service.status
  writes:
    - service.restart
runtime:
  action: restart a service
  action_on_selection: restart it
  order: 3
policy: policy.json
---

## Purpose

Clear a process that is stuck: wedged on a lock, leaking memory, holding
connections it will not release. A restart buys time. It does not fix a bad
version, and it does not fix a dependency that is down.

## Preconditions

- The service name and its environment are known.
- The current status has been read. A service that has already restarted
  several times since its last deployment is crash-looping; restarting it again
  changes nothing and costs another outage window.
- In production, someone has said in writing that the in-flight requests this
  drops are acceptable.

## Steps

1. Read `service.status` and note the version, the replica count and the
   restart count.
2. If the restart count is high and the version is recent, stop and roll back
   instead.
3. Call `service.restart` with the exact service name.
4. Read `service.status` again and compare against what was expected.

## Verification

The read-back must show the service `running` under the same name. A response
to the write is not evidence: the runtime accepts the request before the
process is up. If the read-back shows anything else, report the restart as
unverified and do not repeat it.

## Rollback

A restart cannot be undone. If the service comes back worse, roll the
deployment back to the previous version and treat the restart as the first
entry in the incident timeline.
