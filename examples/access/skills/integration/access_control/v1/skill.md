---
id: integration.access_control
type: integration
version: 1
status: published
description: Puts an account into a group and takes it out again, and reads back the memberships it holds.
entities:
  - entity.user
  - entity.group
operations:
  - name: membership.list
    write: false
    entity: entity.user
    input_schema:
      type: object
      additionalProperties: false
      required: [username]
      properties:
        username:
          type: string
          description: Login name of the account.
        group:
          type: string
          description: Narrow the answer to one group.
    output_schema:
      type: object
      properties:
        ok:
          type: boolean
        memberships:
          type: array
  - name: membership.grant
    write: true
    entity: entity.user
    input_schema:
      type: object
      additionalProperties: false
      required: [username, group]
      properties:
        username:
          type: string
          description: Account to put into the group.
        group:
          type: string
          description: Group to put it into.
    output_schema:
      type: object
      properties:
        ok:
          type: boolean
        membership:
          type: object
    verify:
      operation: membership.list
      expect:
        memberships.0.username: $payload.username
        memberships.0.group: $payload.group
        memberships.0.state: active
    idempotency_fields: [username, group]
  - name: membership.revoke
    write: true
    entity: entity.user
    input_schema:
      type: object
      additionalProperties: false
      required: [username, group]
      properties:
        username:
          type: string
          description: Account to take out of the group.
        group:
          type: string
          description: Group to take it out of.
    output_schema:
      type: object
      properties:
        ok:
          type: boolean
        membership:
          type: object
    verify:
      operation: membership.list
      expect:
        memberships.0.username: $payload.username
        memberships.0.group: $payload.group
        memberships.0.state: revoked
    idempotency_fields: [username, group]
---

## Purpose

The only place membership changes. Both writes name one account and one group,
and both are read back through `membership.list` narrowed to that pair, so what
is reported is the membership itself and not the fact that the call was
accepted.

## Notes

Neither write declares a transition. A membership is not a state of the account
or of the group: the account stays active and the group stays as sensitive as
it was, and what moves is the record that joins the two. The read-back says
which way it moved.

A grant of a membership that is already held, and a revocation of one that was
never held, are both accepted and change nothing. That is why the pair of
account and group is the idempotency key: within a session the second identical
request is answered from the receipt of the first instead of being sent again.
