# Example pack: data pipelines

Four pipelines, the runs an orchestrator started for them and the daily
partitions they write. It is the third reference pack, in a domain a data
engineer works in every morning: why did last night's load fail, can it be
started again, and can a range of days be reloaded now that the source has
been corrected. The core learns what a pipeline, a run and a partition are only
by reading these files.

## What is in it

- `pack.yaml` the manifest, checked against these files by
  `oska pack check examples/data`.
- `skills/` three entities, one integration and three procedures.
- `fixtures/orchestrator.json` the pipelines, runs and partitions the mock
  binding reads and writes.
- `configs/connectors.json` binds the one integration to the mock connector.
- `scenarios/` the conformance suite.

## The domain in one paragraph

A pipeline loads one dataset on a schedule. Each scheduled start is a run for
one partition, a day or an hour of data, and a run ends failed or succeeded. A
failed run can be started again as a new attempt for the same partition, and
nothing else can: a run still in flight would race the new attempt, and one
that succeeded has nothing to redo. A backfill reloads a range of days, which
rewrites data people have already read, so it carries a reason only the person
asking can give, and a range longer than a week needs a second approval.

## How the skills map to the model

**Entities.** `entity.pipeline` is identified by `name` and carries whether its
schedule is in force, plus the last backfill queued against it.
`entity.pipeline_run` is identified by `id` and declares a lifecycle: queued,
running, failed, succeeded, with one move this catalog may make, `run.rerun`
from `failed` back to `queued`. `entity.partition` is one day of a pipeline's
data, complete, stale or missing.

**Integration.** `integration.orchestrator` reads all three and declares two
writes. `run.rerun` names the run and declares the transition to `queued`, so
the read-back through `run.get` has to find the same run in that state.
`pipeline.backfill` names the pipeline, the first day, the number of days and
the reason, and its read-back checks the pipeline holds exactly that backfill,
queued.

**Runbooks.** `runbook.run_diagnosis` answers why a pipeline failed from its
latest run, read-only. `runbook.run_rerun` reads the run, then reruns it on one
approval. `runbook.partition_backfill` reads the pipeline and its partitions
and queues the backfill; `reason` is a human input, so the model may not fill
it in and the runtime asks for it instead. Each runbook carries an
`evaluation.json`.

## Two declarations worth reading

The range of a backfill is a first day and a number of days, not two dates.
That makes its size a field of the payload, so `policy.json` can say in one
condition what needs a second approval (`"days": {"gt": 7}`), and the threshold
lives in that file rather than in the procedure's prose or in the model's
arithmetic.

`run.rerun` is legal only from `failed`, and the state comes from `run.get`,
never from the conversation. A rerun asked for again in the same session is
answered from the ledger before the lifecycle is consulted, which is why the
repeat is a ledger hit rather than a refusal of a run that is now queued.

## The scenarios

Seven sessions under `scenarios/`, run with `oska conform --skills
examples/data/skills --scenarios examples/data/scenarios --connectors
examples/data/configs/connectors.json`. They reach conformance level 2: the
greeting; an off-scope request about data that no procedure covers; why the
nightly orders pipeline failed, routed by its wording alone and answered from
the latest run's error; a failed run rerun on one approval, read back queued,
and asked for again as a ledger hit; a rerun of a run still in flight refused
as an illegal move; a ten-day backfill that asks for its reason and takes two
approvals; and a three-day backfill the person rejects.
