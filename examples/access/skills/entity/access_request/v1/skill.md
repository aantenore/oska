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
initial_state: open
final_states:
  - revoked
  - denied
state_read: request.get
lifecycle:
  - from: [open]
    to: granted
    operation: request.close
    required: true
  - from: [granted]
    to: revoked
    operation: request.close
    offered: false
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

## Lifecycle

A request is raised `open` and is not yet what the person asked for. The move
that completes it is the one to `granted`, recorded by `request.close` once the
membership has actually changed, and it is marked required for that reason: an
open request is unfinished work, and the runtime says so when it reads one.

From `granted` the request can still be settled as `revoked`, by the same
operation, after the membership has been taken away again. That move is legal
and it runs when someone asks for it, but it is never suggested: offering to
revoke an access that was granted a moment ago reads as advice, and the runtime
has no business advising anyone to undo a decision somebody else made.

`revoked` and `denied` are where a request stops. Nothing here writes `denied`:
a request a reviewer turns down is settled by the reviewer, which is why no move
leads to it.

The state is read back through `request.get` before the outcome is recorded. A
request someone else closed while this one was being worked on is the case the
read is there for, and closing it twice is refused rather than written.
