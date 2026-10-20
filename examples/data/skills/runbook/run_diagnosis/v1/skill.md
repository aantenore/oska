---
id: runbook.run_diagnosis
type: runbook
version: 1
status: published
description: Reads the latest run of a pipeline and the error it stopped on, to explain why a run failed without changing anything.
triggers:
  - why did the nightly pipeline fail
  - what broke in last night's load
  - show me the error of the latest run
  - did the orders pipeline run last night
  - perché è fallita la pipeline di stanotte
  - qual è l errore dell ultimo run
entities:
  - entity.pipeline
  - entity.pipeline_run
integrations:
  - integration.orchestrator
capabilities:
  reads:
    - pipeline.get
    - run.latest
    - run.list
guardrails:
  - guardrail.declared_operations_only
  - guardrail.identifiers_are_never_invented
runtime:
  action: find out why a pipeline run failed
  action_on_selection: find out why it failed
  order: 1
---

## Purpose

Answer the first question of every morning a load goes wrong: which run
failed, at which task, and with what error. Read-only, so it is the step to
take before deciding whether to rerun, backfill or leave it for the owner.

## Preconditions

- The pipeline is known by name. "The nightly orders pipeline" is
  `orders_nightly`; a description that could match two pipelines is asked
  about, not guessed.

## Steps

1. Read `pipeline.get` for the pipeline. Note its schedule and its owner, and
   whether it is paused.
2. Read `run.latest` for the pipeline. Note the run identifier, the partition,
   the state, the failed task and the error.
3. When the question is whether this has happened before, read `run.list` for
   the pipeline, narrowed to `failed` if that is the question.
4. Report the run by identifier, the partition it was loading, the task that
   failed and the error as the orchestrator recorded it. Say what the error
   points to only as far as the error itself says.

## Verification

Nothing was changed, so there is nothing to read back. The reads are the
evidence, reported as what the orchestrator held when they ran.

## Rollback

Not applicable.
