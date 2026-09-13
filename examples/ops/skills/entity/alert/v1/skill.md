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
