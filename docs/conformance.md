# Conformance

A scenario is one session written down: the answers the model gives, the turns a
person types, and what each turn must produce. The suite is how you decide
whether an installation behaves the way the standard says it should, and it is
the fastest way to tell whether a change broke something.

## Running it

```sh
opsdeck conform
opsdeck conform --scenarios examples/ops/scenarios
opsdeck conform --json
opsdeck conform --skills examples/ops/skills
```

Without `--scenarios` the directory comes from `OPSDECK_SCENARIOS_DIR`, which
defaults to `examples/ops/scenarios`. The command exits non-zero when any
scenario fails, so it drops into a build without further ceremony.

### The two reference catalogs

A suite belongs to the catalog it was written for, so there are two runs, not
one. The operational catalog is the default: `opsdeck conform` runs its
twenty-one scenarios and reports conformance level 3. The access catalog needs
its own roots, its own scenarios and its own connector bindings, and `--skills`
replaces the catalog roots rather than adding to them, which is what keeps
either suite from seeing the other's procedures:

```sh
opsdeck conform --skills examples/access/skills \
  --scenarios examples/access/scenarios \
  --connectors examples/access/configs/connectors.json
```

Seven scenarios, conformance level 2. That is every level it declares a
scenario for: the evolution lifecycle of level 3 is a property of the runtime
rather than of a domain, and is exercised once, by the operational suite.

The text report is one line per scenario, then the failures underneath, then the
totals:

```
pass  02_restart_staging  level 2  3 turn(s)
...
level 1: 5/5 scenarios pass
level 2: 15/15 scenarios pass
level 3: 1/1 scenarios pass
conformance level 3
```

`--json` returns the same thing as data: `passed`, `level`, `by_level` and the
per-scenario results with their failure lists.

Each scenario runs against its own state directory, created under a temporary
root and thrown away afterwards. Nothing a scenario writes touches
`state/`, and no scenario can see what another one did. The audit sink is in
memory. The provider is always the scripted one, so the suite runs offline and
gives the same answer every time.

The same scenarios run under `pytest` through
`tests/scenarios/test_scenarios.py`, parametrized over the directory.

## The scenario file

One JSON file per scenario, read in file-name order. The name defaults to the
file stem. The normative schema is `spec/schemas/scenario.schema.json`.

```json
{
  "name": "02_restart_staging",
  "description": "A restart outside production: exact-payload approval, verified receipt, and a second identical request that the ledger stops.",
  "level": 2,
  "provider_script": [ ... ],
  "turns": [ ... ],
  "learning": { ... }
}
```

`level` defaults to 2 and decides which bucket the result counts in.

### `provider_script`

The answers the model gives, in order, one step per call. A step is:

```json
{
  "purpose": "router classification",
  "expect_contains": "the search service in staging needs a restart",
  "response": { "kind": "new_task", "runbook_id": "runbook.service_restart",
                "confidence": 0.9, "language": "en" }
}
```

`response` is the object the provider returns. `purpose` is a label that shows
up in the error when a script runs out. `expect_contains` is discussed below.

The script is spent in order, whatever the call was: a classification, a plan, a
tie break, a reply, a draft. That is why the order of the steps mirrors the
order of the model calls in the turn — see
[architecture.md](architecture.md#model-calls-in-order-by-route-kind).

### `turns`

```json
{
  "user": "the search service in staging needs a restart, it is wedged again",
  "expect": {
    "kind": "new_task",
    "contains": ["approval", "service.restart", "{\"service\":\"search\"}"],
    "outcomes": ["read", "approval_pending"],
    "pending": "approval",
    "receipts": 0,
    "verification": null
  },
  "connector_behaviours": { ... }
}
```

| Key | Checked against |
| --- | --- |
| `kind` | The `kind` of the `route` event of the turn, or `guardrail` when there was none. Always checked |
| `contains` | Substrings the reply must hold. Each missing one is a separate failure |
| `outcomes` | The outcome kinds of the turn, in order, exactly. Omit to skip |
| `pending` | `"approval"`, `"input"` or `null`. Always checked, including the null case |
| `receipts` | How many receipts the session holds after the turn. Omit to skip |
| `verification` | The verification status of the most recent receipt, or `null` when there is none. Always checked |

Outcome kinds are `read`, `receipt`, `ledger_hit`, `denied`, `rejected`,
`invalid_plan`, `approval_pending` and `cancelled`.

`kind` has one value that is not a route kind. A guardrail at the input
boundary ends the turn before it is classified, so there is no `route` event to
read; the runner reports `guardrail` instead, taken from the `guardrail` event
whose action was `blocked`. A scenario asserting on it is asserting that the
turn never reached classification at all. The outcome of such a turn is
`denied`, and the reply is the guardrail's own description, so `contains` is
written against the catalog document rather than against anything a script
said.

Note the two that are always checked even when you leave them out: `pending` and
`verification` default to `null`, so a turn that unexpectedly leaves an approval
waiting, or unexpectedly produces a receipt, fails a scenario that says nothing
about it. That is deliberate. A scenario is a statement about a turn, not a
selection of the parts of it you happened to look at.

### `connector_behaviours`

Applied before the turn runs, keyed by integration id and then by operation.
They are how a scenario makes a system misbehave.

```json
"connector_behaviours": {
  "integration.container_runtime": {
    "service.status": { "override": { "ok": true, "service": { "status": "degraded" } } }
  }
}
```

| Behavior | Effect |
| --- | --- |
| `{"fail": true, "error": "..."}` | The operation returns a failure |
| `{"fail_after": 1, "error": "..."}` | It succeeds that many times, then fails |
| `{"override": {...}, "ok": true}` | It returns this data instead of reading the fixture |
| `null` | Clears the behavior in force for that operation |

A behavior stays in force until a later turn replaces or clears it, which is how
a verification-failure scenario makes the write succeed and the read-back
disagree.

The mock connector is the only one a scenario drives, and it works on an
in-memory copy of its fixture, so a scenario changes state and the files on disk
stay as they are.

### `learning`

Runs after the turns, on the session the turns built.

```json
"learning": {
  "draft_from_session": true,
  "provider_script": [ { "purpose": "drafting", "response": { ... } } ],
  "expect_refused": { "to": "submitted", "by": "" },
  "transitions": [
    { "to": "submitted", "by": "s.reyes", "note": "ran it once" },
    { "to": "approved",  "by": "s.reyes" },
    { "to": "published", "by": "s.reyes" }
  ],
  "expect": {
    "state": "published",
    "published_id": "runbook.service_restart",
    "quality_passed": true
  }
}
```

`provider_script` here is spent by a provider of its own, so the wording of the
draft is scripted separately from the turns; without it the draft comes from the
recorded operations alone. `expect_refused` is attempted first, while the
proposal is still a draft, so what refuses it is the missing actor and not an
edge that leads nowhere; the runner also checks that the refused move left the
state untouched. `transitions` are then applied in order. `expect` checks the
final state, optionally that the quality check passed, and optionally that the
catalog now holds the published id — read back from the publication root, which
the runner adds as a catalog root for any scenario with a learning block.

## Levels

| Level | Name | What it covers |
| --- | --- | --- |
| 1 | Declared | The catalog validates, the capability wording is derived from it, the runtime answers offline against a scripted provider |
| 2 | Governed | Also the whole write path: policy, exact-payload approval, second approval, verification, ledger, cancellation, session limits |
| 3 | Adaptive | Also the evolution layer: drafting, quality gates, the lifecycle with named actors, publication |

The level a suite attains is the highest level whose scenarios all pass and
whose lower levels all pass too. One failing level 1 scenario caps the report at
0, however many level 3 scenarios pass, because a system that cannot say what it
does correctly has nothing to govern.

The twenty-one scenarios in `examples/ops/scenarios/` cover greeting, off scope,
language, a governed write with approval and verification and a ledger replay, a
production write needing a second approval, a rejection, goal replacement, a
policy denial, a verification failure, an invalid plan, a cancelled approval, a
session limit, an approval with nothing pending, a composite, a refusal at the
second policy evaluation, and the learning lifecycle.

Five of them are newer categories and are worth naming separately, because each
asserts something the others cannot:

- **An illegal lifecycle move.** A rollback asked for on a deployment that has
  already been rolled back. The reads run, the plan is built, and the write
  ends as `invalid_plan` with no approval requested and no receipt — which is
  what distinguishes a lifecycle refusal from a policy denial.
- **The state of another object.** The same refusal, with a deployment for a
  second service read earlier in the same session. A state read for one object
  does not answer for another, which is the thing a lifecycle check is easiest
  to get wrong.
- **A guardrail at the input boundary.** A request carrying what looks like a
  credential. `kind` is `guardrail`, the script is empty, and the reply is the
  guardrail's own wording: the turn never reached classification, so there was
  nothing for a model to answer.
- **A reply in another language.** The router reports the language once, the
  session keeps it, and the turn that approves the write is decided by the rule
  layer without a model call and is still answered in the language the session
  settled on.
- **A human input.** The text of an incident note is asked for, stored against
  the field, and written exactly as it was given; the same request again
  returns the receipt the ledger already holds.

The access suite adds one more: a request read while it is open is offered the
move that completes it, and one already granted is offered nothing, because the
move it has left is declared as never suggested.

## Writing a scenario for a new domain

1. **Get the turns right by hand first.** Run the session with `opsdeck run`
   against the scripted provider you are about to write, or against a real one,
   and read what comes back. The reply text you will assert on is the reply the
   runtime actually produces.
2. **Write the script in call order.** One step per model call. Start with the
   classification for the first turn, then the plan, then the reply. Add
   `purpose` to every step: when a script runs out, the error names the last
   step that was consumed, and a labeled script tells you immediately which call
   went missing.
3. **Assert on outcomes before text.** `outcomes`, `pending`, `receipts` and
   `verification` are structural and stable. `contains` is about the wording,
   and the wording of an outcomes reply is written by the provider — in a
   scenario, by your own script, so assert on the parts you wrote.
4. **Assert on the payload.** A governed write scenario should have the
   canonical payload rendering in `contains`, exactly as the approval prints it:
   `{"service":"search"}`, no spaces. That is the one string that proves the
   user was shown what was actually sent.
5. **Put the second identical request in.** A write scenario that does not ask
   for the same write twice has not tested the ledger.
6. **Set the level to what the scenario actually exercises.** A scenario with no
   write is level 1. A scenario with a write and its governance is level 2. Only
   a scenario with a learning block is level 3.

## Recording a real session

A script written by hand states what you believe the model will answer. A
cassette states what it answered. Recording one is how the repair loop and the
reply constraints get exercised against a real endpoint once, and then replayed
offline for good.

Point the settings at the endpoint first:

```sh
export OPSDECK_MODEL_BASE_URL=...
export OPSDECK_MODEL_API_KEY=...
export OPSDECK_MODEL_CHAT=...
export OPSDECK_MODEL_EMBEDDING=...
```

Then run the turns, recording every call:

```sh
opsdeck run --provider openai_compatible \
  --session restart --record cassettes/restart.json \
  --text "the search service in staging needs a restart, it is wedged again"
```

A session is more than one turn, and each `run` is a separate process, so the
approval is a second command with the same `--session` and the same `--record`.
The cassette is read back and added to, so the two turns end up in one file, in
order:

```sh
opsdeck run --provider openai_compatible \
  --session restart --record cassettes/restart.json --text "yes"
```

`--record` wraps whichever provider is in force, so it also records a scripted
run; what it cannot do is record nothing, and `--record` with the provider
`none` stops with that message. The file is rewritten after every call, so a run
that dies half way keeps the steps it already had.

Then seed a scenario with it:

```sh
opsdeck cassette to-script cassettes/restart.json --out /tmp/script.json
```

That keeps the `purpose`, the `expect_contains` and the `response` of every
completion and drops the rest — the requests and the embeddings, which a
scenario does not read. Paste the result in as the `provider_script` of a
scenario file and write the `turns` around it.

Review the `expect_contains` before you commit the scenario. A recording sets
each one to the first sixty characters of the last user message of that call,
which is right for a classification and for a plan but arbitrary for a reply,
where the last message is the outcome brief. Replace those with the fragment
that says what the reply is written from — `"kind": "receipt"`,
`"kind": "ledger_hit"` — as described below. An `expect_contains` you did not
choose asserts that the prompt has not changed; one you chose asserts what you
meant.

## Replaying

```sh
opsdeck run --replay cassettes/restart.json --session restart --text "..."
```

Replaying needs no endpoint and no key. Completions are spent in the recorded
order and checked against `expect_contains` exactly as a scenario script is; a
request the cassette did not hold, or one call too many, stops with a message
naming the cassette. Embeddings are looked up by text, and a text the recording
never saw falls back to the same deterministic hash bag the scripted provider
uses, so a catalog that grew since the recording still ranks instead of failing.
`--record` and `--replay` are not used together.

## Keeping the scripts honest

A scripted provider is a recording, and a recording can drift out of step with
the thing it records. Three mechanisms stop that.

**`expect_contains`.** Each script step may carry a substring the last user
message must hold. Before the step's answer is returned, the provider checks it,
and raises if it does not hold, naming the step, its purpose and an excerpt of
the message that reached it. This turns a script from a queue of answers into an
assertion about the prompts. Use it on every step: the request text for a
classification, a distinctive phrase for a plan, and for a reply-writing step a
fragment of the outcome brief, such as `"kind": "receipt"` or `"kind":
"ledger_hit"` — which asserts that the reply is being written from the outcome
you expect and not from some other one.

**The script must be spent.** A scenario whose script has steps left over fails
with `the script was not fully consumed`. A script that runs out fails at the
call that found no answer.

**Classifications must be routed.** The runner counts the script steps whose
response contains a `kind` field and compares that with the number of `route`
events sourced from the model. They must be equal. Without this, a turn that the
rule layer or the fast path answered would silently hand its classification
answer to the planner, and the scenario would pass while testing something else.

There is a fourth check that is not about scripts but belongs here. After the
turns, the runner reads every receipt of the session and every reply of the
scenario, on every scenario. A receipt no reply names fails the scenario, and so
does a reply that names a receipt id and calls it verified while the receipt
says otherwise. The second half strips the phrases that report a write as
unconfirmed first, so `not verified` is not read as `verified`. A scripted
provider can be made to say anything; between the two halves it can neither hide
a write nor overstate one.
