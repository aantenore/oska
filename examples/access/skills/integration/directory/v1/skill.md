---
id: integration.directory
type: integration
version: 1
status: published
description: Reads accounts and groups as the directory holds them, and who currently belongs to what.
entities:
  - entity.user
  - entity.group
operations:
  - name: user.get
    write: false
    entity: entity.user
    input_schema:
      type: object
      additionalProperties: false
      required: [username]
      properties:
        username:
          type: string
          description: Login name of the account.
    output_schema:
      type: object
      properties:
        ok:
          type: boolean
        user:
          type: object
  - name: user.groups
    write: false
    entity: entity.user
    input_schema:
      type: object
      additionalProperties: false
      required: [username]
      properties:
        username:
          type: string
          description: Login name of the account.
    output_schema:
      type: object
      properties:
        ok:
          type: boolean
        groups:
          type: array
  - name: group.get
    write: false
    entity: entity.group
    input_schema:
      type: object
      additionalProperties: false
      required: [group]
      properties:
        group:
          type: string
          description: Group name.
    output_schema:
      type: object
      properties:
        ok:
          type: boolean
        group:
          type: object
  - name: group.members
    write: false
    entity: entity.group
    input_schema:
      type: object
      additionalProperties: false
      required: [group]
      properties:
        group:
          type: string
          description: Group name.
    output_schema:
      type: object
      properties:
        ok:
          type: boolean
        members:
          type: array
---

## Purpose

The register of who exists and what they belong to. Every question about
current access is answered from here, and nothing here changes anything: the
directory is read, membership is changed through the access control system.

## Notes

`user.groups` and `group.members` are the same fact read from two sides. Use
the one the question is about — what a person can reach, or who can reach a
group — and do not assume one was checked because the other was.

An account the directory does not hold is not an account with no groups: the
read fails, and a name that fails to resolve is usually a display name or a
misspelling, which is worth saying rather than treating as an empty answer.
