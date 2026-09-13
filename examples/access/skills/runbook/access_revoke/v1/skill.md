---
id: runbook.access_revoke
type: runbook
version: 1
status: published
description: Takes an account out of a group and confirms the membership is no longer held.
triggers:
  - remove this person from the group
  - he has left the team, take the access away
  - she should not be in that group any more
  - clean up the access of the leaver
  - rimuovi questa persona dal gruppo
  - togli l accesso al gruppo
entities:
  - entity.user
  - entity.group
integrations:
  - integration.directory
  - integration.access_control
capabilities:
  reads:
    - group.get
    - user.groups
  writes:
    - membership.revoke
runtime:
  action: remove a person from a group
  action_on_selection: remove them from a group
  order: 3
policy: policy.json
---

## Purpose

Take away access that is no longer needed: someone changed team, finished the
piece of work the access was for, or left. Narrowing what an account can reach
is the safe direction, which is why it needs one approval and not two, but it
is not free: the membership taken away is the one someone may be relying on
this afternoon.

## Preconditions

- The account is known by username and the group by name.
- The membership is actually held. Read it first, or the revocation is a write
  that changes nothing and reports success.
- Someone knows what stops working. For a leaver that is nobody; for a change of
  team it is worth one sentence before the write.

## Steps

1. Read `user.groups` for the account and confirm the group is in the list.
2. Read `group.get` for the group when the name is ambiguous or the effect is
   not obvious.
3. Call `membership.revoke` with the username and the group.
4. Read the membership back and confirm it is no longer active.

## Verification

The read-back must show the membership for that account and that group as
revoked. Anything else, including a read that finds no membership at all, is
reported as unconfirmed: a membership nobody can find is not the same fact as
one the system says it has taken away.

## Rollback

Grant the membership again with `runbook.access_grant`. It goes through the
same policy as any other grant, so a privileged group revoked by mistake is not
restored on the strength of the mistake.
