---
id: entity.${entity}
type: entity
version: 1
status: published
description: ${A_entity} tracked by this pack, identified by name and moved through a simple lifecycle.
fields:
  - name: name
    type: string
    identifier: true
    description: Stable ${entity} identifier, unique within the estate.
  - name: owner
    type: string
    description: Who is accountable for this ${entity}.
  - name: status
    type: string
    description: "Current lifecycle state: active or archived."
state_field: status
states:
  - active
  - archived
initial_state: active
final_states:
  - archived
state_read: ${entity}.get
lifecycle:
  - from: [active]
    to: archived
    operation: ${entity}.archive
    required: false
    offered: true
---

## Purpose

The record every procedure in this pack points at. Identify it by `name`; a
${entity} starts `active` and moves to `archived` once nothing needs it. Rename
this file's identifier and fields for the real record this pack is about, and
keep the state field and lifecycle only if the real thing genuinely has one.

## Notes

`owner` is here so a real system has somewhere to say who is accountable. Drop
it, or add the fields the real record actually carries.
