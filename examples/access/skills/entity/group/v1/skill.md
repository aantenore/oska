---
id: entity.group
type: entity
version: 1
status: published
description: A named set of accounts that carries a set of permissions, and how much reach belonging to it gives.
fields:
  - name: name
    type: string
    identifier: true
    description: Group name, unique across the directory.
  - name: description
    type: string
    description: What belonging to the group allows, in one line.
  - name: sensitivity
    type: string
    description: "How much reach membership carries: standard or privileged."
state_field: sensitivity
states:
  - standard
  - privileged
relations:
  - name: owner
    target: entity.user
    cardinality: one
    description: Account answerable for who belongs to this group.
noun: group
plural: groups
display_fields: [name, sensitivity]
comparable_fields: [sensitivity]
---

## Purpose

What access is actually granted: permissions are attached to groups, and a
person holds them by belonging. Identify a group by `name`.

## Notes

`sensitivity` is the field every decision about a group turns on. A privileged
group can read or change what other people did, so membership of one is
reviewed by someone other than the person asking for it. Read the group before
proposing to change anyone's membership of it; the name alone does not say how
far it reaches.

The `owner` is the account that answers for the membership list. It is who a
question about a doubtful request goes to, not a second approver by itself.
