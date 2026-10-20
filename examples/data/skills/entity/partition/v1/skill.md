---
id: entity.partition
type: entity
version: 1
status: published
description: One day of the data a pipeline writes, whether it is complete, stale or missing, and how many rows it holds.
fields:
  - name: id
    type: string
    identifier: true
    description: Pipeline name and date, joined by a slash.
  - name: pipeline
    type: string
    description: Pipeline that writes this partition.
  - name: date
    type: string
    description: The day the partition holds, as YYYY-MM-DD.
  - name: state
    type: string
    description: "complete, stale when its source changed after it was loaded, or missing."
  - name: rows
    type: integer
    description: Rows the partition held after its last load.
state_field: state
states:
  - complete
  - stale
  - missing
relations:
  - name: pipeline
    target: entity.pipeline
    cardinality: one
    description: Pipeline that writes this partition.
noun: partition
plural: partitions
display_fields: [id, date, state, rows]
comparable_fields: [state, rows]
---

## Purpose

The unit a backfill is measured in. A backfill reloads a range of days, and
each day is one partition, so the question before a backfill is which of those
days are actually stale or missing.

## Notes

`stale` means the source changed after the partition was loaded, which is the
usual reason to backfill. `missing` means no run has written the day yet; a
failed run leaves its partition missing, and rerunning that run is usually
the narrower fix.

`rows` is what the last load wrote, not what the source holds today.
