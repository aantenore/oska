---
id: entity.access_request
type: entity
version: 1
status: published
description: A recorded ask for one account to be put into one group, and how that ask was settled.
fields:
  - name: id
    type: string
    identifier: true
    description: Request identifier.
  - name: user
    type: string
    description: Account the request is about.
  - name: group
    type: string
    description: Group the request is about.
  - name: state
    type: string
    description: "Lifecycle state: open, granted, revoked or denied."
  - name: justification
    type: string
    description: Reason given when the request was raised.
state_field: state
states:
  - open
  - granted
  - revoked
  - denied
---

## Purpose

The paper trail. A membership that changed without a request behind it is the
thing an access review finds and nobody can explain, so the request is opened
first and closed with what was actually done.

## Notes

A request is closed once, and it is closed after the membership has changed,
never before: a request marked granted while the account is still outside the
group is worse than one left open, because the next review believes it.

`justification` is the sentence the person asking wrote. It is evidence, not a
decision, and it is never rewritten on their behalf.
