---
id: runbook.log_search
type: runbook
version: 1
status: published
description: Returns the log lines of a service that match a pattern, for a stated number of recent lines.
triggers:
  - search the logs for timeout
  - find errors in the api logs
  - grep the logs of a service
  - show log lines matching a pattern
  - cerca negli log del servizio
  - cerca errori nei log
entities:
  - entity.service
integrations:
  - integration.container_runtime
capabilities:
  reads:
    - service.logs
guardrails:
  - guardrail.declared_operations_only
  - guardrail.identifiers_are_never_invented
runtime:
  action: search a service's logs
  action_on_selection: search its logs
  order: 2
---

## Purpose

Find the lines that matter. A tail is enough to see that something is wrong; a
pattern is what turns "it is failing" into a cause.

## Preconditions

- The service name is known.
- A pattern is known. Without one this is a tail, not a search: use the health
  check instead.

## Steps

1. Read `service.logs` with the service, the pattern and a line budget. Start
   at 200 lines; widen only if nothing matches.
2. Report the matching lines verbatim, oldest first, with the count.
3. If nothing matches, say so and say how many lines were searched. An empty
   result over 200 lines is not the same as an empty result over 50.

## Verification

None to perform: nothing was changed.

## Rollback

Not applicable.
