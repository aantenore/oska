---
id: integration.orchestrator
type: integration
version: 1
status: published
description: Reads pipelines, their runs and their partitions, reruns a failed run and queues a backfill over a range of days.
entities:
  - entity.pipeline
  - entity.pipeline_run
  - entity.partition
operations:
  - name: pipeline.get
    write: false
    entity: entity.pipeline
    input_schema:
      type: object
      additionalProperties: false
      required: [pipeline]
      properties:
        pipeline:
          type: string
          description: Pipeline name.
    output_schema:
      type: object
      properties:
        ok:
          type: boolean
        pipeline:
          type: object
  - name: run.latest
    write: false
    entity: entity.pipeline_run
    input_schema:
      type: object
      additionalProperties: false
      required: [pipeline]
      properties:
        pipeline:
          type: string
          description: Pipeline whose most recent run is wanted.
    output_schema:
      type: object
      properties:
        ok:
          type: boolean
        pipeline_run:
          type: object
  - name: run.get
    write: false
    entity: entity.pipeline_run
    input_schema:
      type: object
      additionalProperties: false
      required: [run_id]
      properties:
        run_id:
          type: string
          description: Run identifier.
    output_schema:
      type: object
      properties:
        ok:
          type: boolean
        pipeline_run:
          type: object
  - name: run.list
    write: false
    entity: entity.pipeline_run
    input_schema:
      type: object
      additionalProperties: false
      properties:
        pipeline:
          type: string
          description: Restrict the list to the runs of one pipeline.
        state:
          type: string
          enum: [queued, running, failed, succeeded]
          description: Restrict the list to one state.
        limit:
          type: integer
          minimum: 1
          maximum: 50
          description: How many runs to return, most recent last.
    output_schema:
      type: object
      properties:
        ok:
          type: boolean
        pipeline_runs:
          type: array
  - name: run.rerun
    write: true
    entity: entity.pipeline_run
    transitions:
      state: queued
    input_schema:
      type: object
      additionalProperties: false
      required: [run_id]
      properties:
        run_id:
          type: string
          description: The failed run to start again.
    output_schema:
      type: object
      properties:
        ok:
          type: boolean
        pipeline_run:
          type: object
    verify:
      operation: run.get
      expect:
        pipeline_run.id: $payload.run_id
    idempotency_fields: [run_id]
  - name: partition.list
    write: false
    entity: entity.partition
    input_schema:
      type: object
      additionalProperties: false
      required: [pipeline]
      properties:
        pipeline:
          type: string
          description: Pipeline whose partitions are wanted.
        state:
          type: string
          enum: [complete, stale, missing]
          description: Restrict the list to one state.
        limit:
          type: integer
          minimum: 1
          maximum: 92
          description: How many partitions to return, most recent last.
    output_schema:
      type: object
      properties:
        ok:
          type: boolean
        partitions:
          type: array
  - name: pipeline.backfill
    write: true
    entity: entity.pipeline
    input_schema:
      type: object
      additionalProperties: false
      required: [pipeline, start_date, days, reason]
      properties:
        pipeline:
          type: string
          description: Pipeline to backfill.
        start_date:
          type: string
          pattern: "^[0-9]{4}-[0-9]{2}-[0-9]{2}$"
          description: First day to reload, as YYYY-MM-DD.
        days:
          type: integer
          minimum: 1
          maximum: 31
          description: How many consecutive daily partitions to reload, the first day included.
        reason:
          type: string
          minLength: 1
          description: Why the range is reloaded, in the words of the person asking.
    output_schema:
      type: object
      properties:
        ok:
          type: boolean
        pipeline:
          type: object
    verify:
      operation: pipeline.get
      expect:
        pipeline.name: $payload.pipeline
        pipeline.backfill_from: $payload.start_date
        pipeline.backfill_days: $payload.days
        pipeline.backfill_state: queued
    idempotency_fields: [pipeline, start_date, days]
---

## Purpose

The orchestrator is the system of record for what ran, when, and with what
result. Everything a person asks about a pipeline is read here, and the two
things this catalog may change go through here too.

## Notes

`run.rerun` starts a failed run again as a new attempt under the same
identifier and for the same partition. The orchestrator accepts the call for
any run; whether it is a legal move is decided by the lifecycle of the run,
before the call is sent. The read-back is `run.get` for the same run, and the
state the move promises, `queued`, is part of it: a run that reads back failed
was not queued again, whatever the response said.

`pipeline.backfill` queues a reload of a range of days, named by its first day
and its length rather than by two dates, so the size of the range is a number
the policy can read instead of an arithmetic the model has to get right. The
read-back checks that the pipeline holds this backfill, from this day, for
this many days, queued.

A backfill does not declare a transition. Queuing one does not change whether
the pipeline's schedule is in force, and the fields that say what was queued
are read back explicitly.
