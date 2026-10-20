---
id: entity.pipeline_run
type: entity
version: 1
status: published
description: One execution of a pipeline for one partition, how far it got, and the error it stopped on.
fields:
  - name: id
    type: string
    identifier: true
    description: Run identifier, unique within the orchestrator.
  - name: pipeline
    type: string
    description: Pipeline the run belongs to.
  - name: partition
    type: string
    description: The slice of data the run loads, a day or an hour.
  - name: state
    type: string
    description: "Lifecycle state: queued, running, failed or succeeded."
  - name: attempt
    type: integer
    description: How many times the run has been started, the first one included.
  - name: latest
    type: boolean
    description: True for the most recent run of its pipeline.
  - name: started_at
    type: string
    description: When the current attempt started.
  - name: finished_at
    type: string
    description: When the current attempt ended, empty while it runs.
  - name: failed_task
    type: string
    description: The task inside the run that failed, empty unless the run failed.
  - name: error
    type: string
    description: The error that task stopped on, as the orchestrator recorded it.
state_field: state
states:
  - queued
  - running
  - failed
  - succeeded
initial_state: queued
final_states:
  - succeeded
state_read: run.get
lifecycle:
  - from: [failed]
    to: queued
    operation: run.rerun
relations:
  - name: pipeline
    target: entity.pipeline
    cardinality: one
    description: Pipeline this run belongs to.
noun: pipeline run
plural: pipeline runs
display_fields: [id, pipeline, partition, state]
comparable_fields: [state, attempt]
---

## Purpose

What actually happened on a given night. A pipeline is a schedule; a run is
the evidence. Identify a run by `id`. "Last night's run" names a run only once
the orchestrator has said which one was latest for that pipeline.

## Notes

`error` and `failed_task` are the orchestrator's words, not a diagnosis. A lock
timeout says another session held the table; it does not say which one, or
whether it will hold it again tonight.

## Lifecycle

A run starts `queued`, moves to `running` when a worker picks it up, and ends
`failed` or `succeeded`. None of those moves is this catalog's to make: they
are states a run is found in, and the orchestrator is what puts it there.

One move is. `run.rerun` takes a run that `failed` and queues it again as a
new attempt of the same run, for the same partition. It is legal from `failed`
and from nothing else: a run that is still queued or running has not finished,
and a rerun of it would race the attempt already in flight, while a run that
`succeeded` is where the record ends, so nothing moves out of it.

The state is read through `run.get` for the run the rerun names, never taken
from the conversation. Someone else may have rerun it already, and a run that
is queued again is no longer failed.
