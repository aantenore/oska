---
id: entity.user
type: entity
version: 1
status: published
description: A person with an account in the directory, the team they belong to and whether the account can be used.
fields:
  - name: username
    type: string
    identifier: true
    description: Login name, unique across the directory and never reused.
  - name: display_name
    type: string
    description: Name the person is known by, as it appears in the directory.
  - name: department
    type: string
    description: Team the account is registered to.
  - name: status
    type: string
    description: "Current account state: active or suspended."
state_field: status
states:
  - active
  - suspended
relations:
  - name: memberships
    target: entity.group
    cardinality: many
    description: Groups this account currently belongs to.
---

## Purpose

The person access is granted to and taken away from. Identify an account by
`username`: display names repeat across departments and change when people
change theirs, and a request that names only a display name is not yet a
request about an account.

## Notes

`status` says whether the account can be used, not what it can reach. A
suspended account keeps every group it was in, which is why the first question
about a leaver is what they are still a member of and not whether they can log
in.
