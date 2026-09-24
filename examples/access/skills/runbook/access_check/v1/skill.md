---
id: runbook.access_check
type: runbook
version: 1
status: published
description: Reports what an account belongs to and how much reach those groups carry, without changing anything.
triggers:
  - what access does this person have
  - which groups is she in
  - show me what he can reach
  - is this group a privileged one
  - che accessi ha questa persona
  - a quali gruppi appartiene
entities:
  - entity.user
  - entity.group
integrations:
  - integration.directory
capabilities:
  reads:
    - user.get
    - user.groups
    - group.get
guardrails:
  - guardrail.declared_operations_only
  - guardrail.identifiers_are_never_invented
subject:
  entity: entity.user
  cardinality: one
  joins: focus
presentation:
  on_single_result:
    view: account
    bind:
      user: $item.username
      name: $item.display_name
      status: $item.status
      groups: $item.groups
runtime:
  action: check what access a person has
  action_on_selection: check their access
  order: 1
---

## Purpose

Answer the question every other procedure here starts from: which account is
this, what does it belong to now, and does any of it carry reach that a change
would have to be reviewed for. Read-only, so it costs nothing to run before
deciding anything.

## Preconditions

- The account is known by username. A display name is not enough; two people
  share one often enough that acting on the wrong account is a real outcome.

## Steps

1. Read `user.get` for the username. Note the department and whether the
   account is active or suspended.
2. Read `user.groups` for the same username.
3. When the question is about one group, read `group.get` for it and note its
   sensitivity and its owner.
4. Report the groups by name, say which of them are privileged, and say what
   the account's status is. Do not summarize the list away: the names are the
   answer.

## Verification

Nothing was changed, so there is nothing to read back. The reads are the
evidence, and they are reported as what the directory held when they ran.

## Rollback

Not applicable.
