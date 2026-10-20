---
id: runbook.run_rerun
type: runbook
version: 1
status: published
description: Starts a failed pipeline run again as a new attempt for the same partition, and confirms the orchestrator queued it.
triggers:
  - rerun the failed run
  - retry last night's load
  - start that run again
  - kick off the failed orders run once more
  - rilancia il run fallito
  - riprova il caricamento di stanotte
entities:
  - entity.pipeline_run
integrations:
  - integration.orchestrator
capabilities:
  reads:
    - run.get
  writes:
    - run.rerun
guardrails:
  - guardrail.declared_operations_only
  - guardrail.identifiers_are_never_invented
  - guardrail.exact_payload_approval
  - guardrail.read_back_before_confirmation
runtime:
  action: rerun a failed pipeline run
  action_on_selection: rerun it
  order: 2
policy: policy.json
---

## Purpose

Get a failed partition loaded without waiting for the next scheduled run. A
rerun repeats the work the run was already meant to do, for the same
partition, which is why it needs one approval and not two.

## Preconditions

- The run is known by identifier. "Last night's run" is resolved first, by
  reading the latest run of the pipeline.
- The run failed. A run that is queued or running has not finished, and one
  that succeeded has nothing to redo; both are refused before anything is sent.
- Whatever made it fail is understood well enough to expect a different
  result. A lock timeout may clear on its own; a missing source file will not.

## Steps

1. Read `run.get` for the run. Note its state, the partition and the attempt.
2. Call `run.rerun` with the run identifier.
3. Read the run back and confirm the orchestrator holds it as queued.

## Verification

The read-back must show the same run, queued. The response to the call says
only that the orchestrator accepted it. A run that reads back failed, or in any
state other than queued, is reported as unconfirmed and is not sent again.

## Rollback

A queued attempt can be cancelled in the orchestrator by its owner. Nothing
here does that: the partition it loads is the same one the failed attempt
left missing, so letting it run is rarely worse than stopping it.
