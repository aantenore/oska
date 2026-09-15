---
id: composite.incident_triage
type: composite
version: 1
status: published
description: "Triages a service under an open incident: reads its health, searches its logs, then records what was found."
triggers:
  - triage the incident on checkout
  - what is going on with this incident
  - investigate the incident and write it up
  - run triage for the api incident
  - analizza l incidente
  - fai il triage dell incidente
runbooks:
  - runbook.service_health
  - runbook.log_search
  - runbook.incident_note
guardrails:
  - guardrail.declared_operations_only
  - guardrail.identifiers_are_never_invented
  - guardrail.exact_payload_approval
  - guardrail.read_back_before_confirmation
runtime:
  action: triage an incident on a service
  action_on_selection: triage it
  order: 7
---

## Purpose

The first ten minutes of a response, in a fixed order: establish what the
service is doing, find the lines that explain it, and write both down on the
incident before anyone starts changing things.

## Preconditions

- An incident is open and its identifier is known.
- The service the incident was raised against is known.
- A search pattern is known, or the health check produces one.

## Steps

1. Run `runbook.service_health` for the service. Note version, replicas and
   restart count.
2. Run `runbook.log_search` with a pattern taken from the incident summary or
   from the errors the health check surfaced.
3. Run `runbook.incident_note` with one line that carries the status, the
   version and the most telling log line.

Each step runs under its own policy. The note is a write and stops for approval
on its own terms, even though the two steps before it did not.

## Verification

Steps one and two change nothing and are their own evidence. Step three is
verified by reading the incident back and finding the note in place. Triage is
complete only when the note is recorded.

## Rollback

Nothing to undo. If the triage was written against the wrong incident, append a
correcting note to both.
