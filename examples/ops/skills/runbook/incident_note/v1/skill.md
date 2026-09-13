---
id: runbook.incident_note
type: runbook
version: 1
status: published
description: Appends one note to the timeline of an open incident, after reading the incident back.
triggers:
  - add a note to the incident
  - write this down on the incident
  - record what we just did on the incident
  - update the incident timeline
  - aggiungi una nota all incidente
  - annota sull incidente
entities:
  - entity.incident
integrations:
  - integration.incident_tracker
capabilities:
  reads:
    - incident.get
  writes:
    - incident.note
runtime:
  action: add a note to an incident
  action_on_selection: add a note to it
policy: policy.json
---

## Purpose

Keep the timeline honest while the response is happening. A note written an
hour later is a reconstruction; a note written now is evidence.

## Preconditions

- The incident identifier is known and the incident is open.
- The note says what was observed or what was done, with the time and the
  service it concerns. No speculation about cause unless it is labelled as
  such.

## Steps

1. Read `incident.get` for the incident and confirm it is open.
2. Draft one line. Facts first: what was seen, what was run, what changed.
3. Call `incident.note` with the incident identifier and the exact text.
4. Read `incident.get` again.

## Verification

The read-back must show the note as the most recent entry on the incident. If
the note is missing, write it again only after confirming it was not recorded;
the same text is written once per incident.

## Rollback

A note cannot be removed. Correct it by appending a second note that says what
the first one got wrong.
