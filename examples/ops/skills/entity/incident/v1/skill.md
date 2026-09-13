---
id: entity.incident
type: entity
version: 1
status: published
description: An open or closed incident raised against a service, with its severity and its running notes.
fields:
  - name: id
    type: string
    identifier: true
    description: Incident identifier.
  - name: service
    type: string
    description: Service the incident was raised against.
  - name: severity
    type: string
    description: Severity from sev1 (worst) to sev3.
  - name: state
    type: string
    description: "Lifecycle state: open, mitigated or closed."
  - name: summary
    type: string
    description: One line describing what is wrong.
  - name: notes
    type: array
    description: Timestamped notes appended during the response.
  - name: last_note
    type: string
    description: Text of the most recent note, kept for read-back after a write.
state_field: state
states:
  - open
  - mitigated
  - closed
relations:
  - name: service
    target: entity.service
    cardinality: one
    description: Service this incident was raised against.
---

## Purpose

The written record of a response. Everything an operator learns while an
incident is open belongs in a note, because the timeline is what the review
after the fact is written from.

## Notes

Notes are append-only. Correcting an earlier note means writing a new one that
says what changed.
