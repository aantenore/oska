---
id: entity.pipeline
type: entity
version: 1
status: published
description: A scheduled job that loads one dataset into the warehouse, who owns it, and the backfill it last queued.
fields:
  - name: name
    type: string
    identifier: true
    description: Pipeline name, unique within the orchestrator.
  - name: description
    type: string
    description: What the pipeline loads, in one line.
  - name: schedule
    type: string
    description: When the orchestrator starts a run on its own.
  - name: owner
    type: string
    description: Team that answers for the data the pipeline writes.
  - name: status
    type: string
    description: "Whether the schedule is in force: active or paused."
  - name: backfill_from
    type: string
    description: First day of the backfill last queued, as YYYY-MM-DD.
  - name: backfill_days
    type: integer
    description: How many daily partitions that backfill covers.
  - name: backfill_reason
    type: string
    description: Why the backfill was asked for, in the words of the person who asked.
  - name: backfill_state
    type: string
    description: Where the orchestrator has that backfill, queued once accepted.
state_field: status
states:
  - active
  - paused
relations:
  - name: runs
    target: entity.pipeline_run
    cardinality: many
    description: Runs the orchestrator started for this pipeline, oldest first.
  - name: partitions
    target: entity.partition
    cardinality: many
    description: The daily slices of data this pipeline writes.
noun: pipeline
plural: pipelines
display_fields: [name, schedule, owner]
comparable_fields: [status, owner]
---

## Purpose

The unit a data engineer talks about: "the nightly orders pipeline", "the
hourly customer copy". Identify a pipeline by `name`; the description and the
schedule are how a person recognizes it, not how the orchestrator does.

## Notes

`status` says whether the schedule is in force, not whether the data is good.
An active pipeline whose last run failed is still active: it will start again
on its own at the next scheduled time, which is worth knowing before rerunning
anything by hand.

The `backfill_` fields describe the last backfill queued against the pipeline.
The reason is kept exactly as it was given, because it is what someone reading
the warehouse a month later needs in order to trust the numbers it reloaded.
