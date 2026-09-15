---
id: runbook.access_grant
type: runbook
version: 1
status: published
description: Puts the signed-in account into a group after reading how much reach that group carries, and confirms it holds.
triggers:
  - add me to the reporting group
  - I need to be in the billing group
  - give me access to the finance tools
  - I cannot see the shared queue, put me in that group
  - mettimi nel gruppo dei report
  - dammi accesso agli strumenti finanziari
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
principal_inputs:
  username: subject
guardrails:
  - guardrail.declared_operations_only
  - guardrail.identifiers_are_never_invented
  - guardrail.exact_payload_approval
  - guardrail.read_back_before_confirmation
runtime:
  action: add yourself to a group
  action_on_selection: null
  order: 2
policy: policy.json
---

## Purpose

Give a person the access their work needs, and no more than that. What is
granted here is a membership: the permissions come with the group, so the
question worth answering before the write is what the group reaches, not what
the person said they wanted.

The account is not one of those questions. `username` is declared a principal
input, so it is taken from the identity the channel authenticated and never
from the conversation: this procedure grants access to the person asking for
it, and there is no sentence anybody can type that makes it grant access to
somebody else. Access for another person is a different act with a different
reviewer, and it is not this one.

## Preconditions

- Somebody is signed in. A session with no authenticated identity is refused
  here rather than asked whose account to use.
- The group is known by name.
- The group has been read. A privileged group is reviewed by someone other than
  the person asking, and the approval reflects that.
- There is a request, a ticket or a written ask behind it. Access granted from a
  conversation alone is access nobody can account for at the next review.

## Steps

1. Read `group.get` for the group and note its sensitivity and its owner.
2. Read `user.groups` and check the membership is not already held; granting one
   that is already there changes nothing and hides that the ask was already
   satisfied. The account is not named in the payload: the runtime fills it.
3. Call `membership.grant` with the group, exactly as read. The account is
   filled in the same way, after the plan and before the policy judges it.
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
