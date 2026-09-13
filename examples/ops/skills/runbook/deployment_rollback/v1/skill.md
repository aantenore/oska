---
id: runbook.deployment_rollback
type: runbook
version: 1
status: published
description: Puts a service back on an earlier version, defaulting to the one it ran before the last deployment.
triggers:
  - roll back the checkout service
  - revert the api to the previous version
  - undo the last deployment
  - put payments back on the old version
  - torna alla versione precedente
  - annulla l ultimo rilascio
entities:
  - entity.service
  - entity.deployment
integrations:
  - integration.container_runtime
  - integration.deployment_control
capabilities:
  reads:
    - service.status
    - deployment.get
    - deployment.list
  writes:
    - deployment.rollback
runtime:
  action: roll a service back to an earlier version
  action_on_selection: roll it back
  order: 4
policy: policy.json
---

## Purpose

Take a bad release out of the path of customers. This is the shortest route
back to a state that was known to work, and it is almost always the right first
move when a service broke immediately after a deployment.

## Preconditions

- The service name and its environment are known.
- The target version is known, or the current deployment record carries a
  `previous_version` to fall back to.
- The target version has run in this environment before. A version that was
  never deployed here is a forward deployment wearing a rollback's name.
- Any schema or data change shipped with the version being removed has been
  considered: a rollback moves code, not data.

## Steps

1. Read `service.status` for the service and note its environment: the policy
   that governs the rollback is written in terms of it.
2. Read `deployment.get` for the service and note `version` and
   `previous_version`.
3. If no target version was given, use `previous_version`. If that field is
   empty, read `deployment.list` and take the last deployment that succeeded.
4. State the exact service and target version, and get approval.
5. Call `deployment.rollback` with that exact pair.
6. Read `deployment.get` again.

## Verification

The read-back must show the service on the target version with the deployment
in state `rolled_back`. Until it does, the rollback is unverified and the
incident stays open. Check the service is serving traffic as well: a version
can be in place and still be down.

## Rollback

Roll forward. Deploy the version that was removed once the fault in it is
understood and fixed; do not roll back the rollback.
