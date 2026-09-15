---
id: entity.deployment
type: entity
version: 1
status: published
description: A record of one service moving from a previous version to a new one, and how that move ended.
fields:
  - name: id
    type: string
    identifier: true
    description: Deployment identifier.
  - name: service
    type: string
    description: Service the deployment targets.
  - name: version
    type: string
    description: Version the deployment put in place.
  - name: previous_version
    type: string
    description: Version that was running before this deployment.
  - name: state
    type: string
    description: "Lifecycle state: succeeded, failed, rolled_back or in_progress."
  - name: current
    type: boolean
    description: True for the deployment that describes what the service serves now.
state_field: state
states:
  - in_progress
  - succeeded
  - failed
  - rolled_back
initial_state: in_progress
final_states:
  - rolled_back
state_read: deployment.get
lifecycle:
  - from: [in_progress, succeeded, failed]
    to: rolled_back
    operation: deployment.rollback
relations:
  - name: service
    target: entity.service
    cardinality: one
    description: Service this deployment targets.
---

## Purpose

The history of what changed and when. Most incidents start with a deployment,
so the first question in triage is which version a service is on and what it
was on before.

## Notes

Exactly one deployment per service carries `current: true`. `previous_version`
on that record is the rollback target when no explicit version is given.

## Lifecycle

A deployment starts `in_progress`. Whether it ends `succeeded` or `failed` is
decided by the release going out, not by anything in this catalog, which is why
no move here leads to either: they are states a deployment is found in.

One move is this catalog's to make. `deployment.rollback` takes a deployment
that is in progress, that succeeded or that failed, and puts an earlier version
back in its place. Where it lands is `rolled_back`, and that is the end of the
record: a rollback is undone by deploying forward, never by rolling back a
rollback, so nothing moves out of `rolled_back` and a second rollback of the
same deployment is refused before anything is sent.

The state is read back through `deployment.get` for the service, never assumed
from the conversation: the deployment someone is talking about may already have
been rolled back by whoever was working on it first.
