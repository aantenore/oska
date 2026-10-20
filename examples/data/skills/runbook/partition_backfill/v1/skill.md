---
id: runbook.partition_backfill
type: runbook
version: 1
status: published
description: Queues a reload of a pipeline over a range of days, with the reason the person gives, and confirms the orchestrator holds it.
triggers:
  - backfill the orders pipeline for last week
  - reload the partitions from the first of the month
  - the source was corrected, reprocess those days
  - rebuild ten days of data for this pipeline
  - ricarica le partizioni della settimana scorsa
  - fai il backfill della pipeline per questi giorni
entities:
  - entity.pipeline
  - entity.partition
integrations:
  - integration.orchestrator
capabilities:
  reads:
    - pipeline.get
    - partition.list
  writes:
    - pipeline.backfill
human_inputs:
  - reason
guardrails:
  - guardrail.declared_operations_only
  - guardrail.identifiers_are_never_invented
  - guardrail.exact_payload_approval
  - guardrail.read_back_before_confirmation
runtime:
  action: backfill a pipeline over a range of days
  action_on_selection: backfill it
  order: 3
policy: policy.json
---

## Purpose

Reload days whose data is wrong or absent: a source system corrected its
history, a bug in a transformation was fixed, a pipeline was paused through an
outage. A backfill rewrites partitions other people have already read from, so
it is approved on its exact range and recorded with the reason behind it.

## Preconditions

- The pipeline is known by name, and the range by its first day and its number
  of days.
- The reason comes from the person asking. It is what someone reading the
  warehouse later uses to decide whether to trust the reloaded numbers, so it
  is asked for and written exactly as given, never composed on their behalf.
- The days in the range are stale or missing. Reloading complete partitions
  costs warehouse time and changes nothing anyone asked for.
- A range longer than the policy's threshold is reviewed by a second person,
  because it rewrites more history than a correction usually touches.

## Steps

1. Read `pipeline.get` for the pipeline and note whether its schedule is in
   force: a backfill on an active pipeline competes with its scheduled runs.
2. Read `partition.list` for the pipeline and note which days in the range are
   stale or missing.
3. Ask for the reason when it was not given, and use the answer as it is.
4. Call `pipeline.backfill` with the pipeline, the first day, the number of
   days and the reason.
5. Read the pipeline back and confirm it holds that backfill, queued.

## Verification

The read-back must show the pipeline with a backfill from the same first day,
for the same number of days, queued. Anything else is reported as
unconfirmed, and the backfill is not sent a second time on the strength of a
response alone.

## Rollback

A queued backfill can be cancelled in the orchestrator by the pipeline's owner
before it starts. Once partitions are reloaded, the earlier data is gone from
them; a backfill is undone only by another backfill from a corrected source.
