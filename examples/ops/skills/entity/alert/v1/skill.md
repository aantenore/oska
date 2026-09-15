---
id: entity.alert
type: entity
version: 1
status: published
description: A firing or silenced alert, the rule that produced it and the service it points at.
fields:
  - name: id
    type: string
    identifier: true
    description: Alert identifier.
  - name: service
    type: string
    description: Service the alert points at.
  - name: rule
    type: string
    description: Name of the rule that fired.
  - name: state
    type: string
    description: "Lifecycle state: firing, silenced or resolved."
  - name: silenced_until
    type: string
    description: Time the silence expires, empty when the alert is not silenced.
state_field: state
states:
  - firing
  - silenced
  - resolved
initial_state: firing
final_states:
  - resolved
state_read: alert.list
lifecycle:
  - from: [firing]
    to: silenced
    operation: alert.silence
relations:
  - name: service
    target: entity.service
    cardinality: one
    description: Service the rule behind this alert watches.
---

## Purpose

What woke someone up. An alert is a symptom, not a cause: silencing one stops
the paging, it does not change the state of the service the rule watches.

## Notes

A silence has an end. An alert silenced without a matching incident or a fix in
flight will fire again the moment the silence expires.

## Lifecycle

An alert arrives firing. The one move anything here can make is to silence it,
and only from `firing`: an alert already silenced is silenced, and silencing it
again is a second promise nobody made. The way back from `silenced` to `firing`
is not a move of this catalog at all — the silence runs out and the rule fires
again on its own — so no operation claims it.

`resolved` is where an alert stops. Nothing leaves it, and nothing here puts an
alert there; the condition the rule watches does.

The state is never taken from what was said a moment ago. It is read back
through `alert.list` narrowed to the alert, because a silence set by someone
else between two turns is exactly the case that matters.
