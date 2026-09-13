---
id: runbook.access_grant
type: runbook
version: 1
status: published
description: Puts an account into a group after reading how much reach that group carries, and confirms it holds.
triggers:
  - add this person to the group
  - she needs to be in the reporting group
  - give him access to the billing tools
  - put the new joiner in the support group
  - aggiungi questa persona al gruppo
  - dagli accesso al gruppo
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
    - membership.grant
runtime:
  action: add a person to a group
  action_on_selection: add them to a group
  order: 2
policy: policy.json
---

## Purpose

Give a person the access their work needs, and no more than that. What is
granted here is a membership: the permissions come with the group, so the
question worth answering before the write is what the group reaches, not what
the person said they wanted.

## Preconditions

- The account is known by username and the group by name.
- The group has been read. A privileged group is reviewed by someone other than
  the person asking, and the approval reflects that.
- There is a request, a ticket or a written ask behind it. Access granted from a
  conversation alone is access nobody can account for at the next review.

## Steps

1. Read `group.get` for the group and note its sensitivity and its owner.
2. Read `user.groups` for the account and check the membership is not already
   held; granting one that is already there changes nothing and hides that the
   ask was already satisfied.
3. Call `membership.grant` with the username and the group, exactly as read.
4. Read the membership back and compare it against what was asked for.

## Verification

The read-back must show a membership for that account and that group, active.
The response to the write says only that the request was accepted. If the
read-back shows anything else, report the grant as unconfirmed and leave it for
someone to look at rather than sending it again.

## Rollback

Take the membership away with `runbook.access_revoke` and close or reopen the
request to match. A grant that stood for a few minutes still happened, so say
so on the request instead of removing the trace.
