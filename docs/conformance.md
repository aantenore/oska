# Conformance

A scenario is one session written down: the answers the model gives, the turns a
person types, and what each turn must produce. The suite is how you decide
whether an installation behaves the way the standard says it should, and it is
the fastest way to tell whether a change broke something.

## Running it

```sh
oska conform
oska conform --scenarios examples/ops/scenarios
oska conform --json
oska conform --skills examples/ops/skills
```

Without `--scenarios` the directory comes from `OSKA_SCENARIOS_DIR`, which
defaults to `examples/ops/scenarios`. The command exits non-zero when any
scenario fails, so it drops into a build without further ceremony.

### The two reference catalogs

A suite belongs to the catalog it was written for, so there are two runs, not
one. The operational catalog is the default: `oska conform` runs its
twenty-three scenarios and reports conformance level 3. The access catalog
needs its own roots, its own scenarios and its own connector bindings, and
`--skills` replaces the catalog roots rather than adding to them, which is what
keeps either suite from seeing the other's procedures:

```sh
oska conform --skills examples/access/skills \
  --scenarios examples/access/scenarios \
  --connectors examples/access/configs/connectors.json
```

Eight scenarios, conformance level 2. That is every level it declares a
scenario for: the evolution lifecycle of level 3 is a property of the runtime
rather than of a domain, and is exercised once, by the operational suite.

The text report is one line per scenario, then the failures underneath, then the
totals:

```
pass  02_restart_staging  level 2  3 turn(s)
...
level 1: 6/6 scenarios pass
level 2: 16/16 scenarios pass
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
  "connector_behaviors": { ... }
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
`invalid_plan`, `approval_pending`, `cancelled` and `unavailable`. The last is
a capability that could not be reached, which is neither a result nor a
refusal; a scenario that asserts it is asserting that the plan carried on past
it and that nothing was written.

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

### `connector_behaviors`

Applied before the turn runs, keyed by integration id and then by operation.
They are how a scenario makes a system misbehave.

```json
"connector_behaviors": {
  "integration.container_runtime": {
    "service.status": { "override": { "ok": true, "service": { "status": "degraded" } } }
  }
}
```

| Behavior | Effect |
| --- | --- |
| `{"fail": true, "error": "..."}` | The system answers no. Terminal: another attempt would be answered the same way, and the breaker is not touched, because a system that refuses is a system that is up |
| `{"unreachable": true, "error": "..."}` | The call does not reach the system at all. Retryable, and the only injected failure that counts toward the breaker. This is how an outage is simulated |
| `{"fail_after": 1, "error": "..."}` | It succeeds that many times, then fails in whichever of the two ways the behavior declares |
| `{"override": {...}, "ok": true}` | It returns this data instead of reading the fixture |
| `null` | Clears the behavior in force for that operation |

A behavior stays in force until a later turn replaces or clears it, which is how
a verification-failure scenario makes the write succeed and the read-back
disagree, and how an outage scenario makes one turn fail and the next one
succeed.

The difference between `fail` and `unreachable` is the whole of what an
availability scenario is about, so a scenario that means an outage has to say
`unreachable`. A `fail` produces a refusal, settles the intent in the ledger
and leaves the breaker closed; an `unreachable` produces an `unavailable`
outcome, settles nothing, and counts toward opening the breaker.

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
catalog now holds the published id.

The move to `published` composes a change request rather than writing a skill
file, so the runner does what a person would do next: it applies the request
into the publication root, under the actor the transition named, and adds that
root as a catalog root for any scenario with a learning block. That is the one
place in the suite where a review is stood in for, and it is stood in for
explicitly, because a scenario that asserts a published procedure has to have
somebody publish it.

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

The twenty-three scenarios in `examples/ops/scenarios/` cover greeting, off
scope, language, a governed write with approval and verification and a ledger
replay, a production write needing a second approval, a rejection, goal
replacement, a policy denial, a verification failure, an invalid plan, a
cancelled approval, a session limit, an approval with nothing pending, a
composite, a refusal at the second policy evaluation, and the learning
lifecycle.

Seven of them are newer categories and are worth naming separately, because each
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
- **A capability that is unavailable.** One read is made unreachable for one
  turn. The turn records an `unavailable` outcome, answers from the read that
  did come back, writes nothing, and says the capability may be tried again;
  the next turn, with the behavior cleared, runs the same check normally. One
  failure is below the run that opens the breaker, so the binding is reported
  as unavailable without being taken out of service.
- **Labels in the language of the turn.** A request in Italian is offered its
  next steps in Italian. The actions travel to the reply writer as a key and
  the wording their author declared and come back rewritten by the same call
  that wrote the reply, so the offer is the catalog's and the language is the
  conversation's.

The access suite adds two more: a request read while it is open is offered the
move that completes it, and one already granted is offered nothing, because the
move it has left is declared as never suggested; and a procedure that takes the
account from the authenticated identity is refused to a session nobody signed
in to, with `invalid_plan` and no question asked about which account was meant.

## What a catalog change runs

The suite is one of three things a change to a catalog should pass, and they
answer different questions.

```sh
oska validate          # does the catalog hold up against itself
oska evaluate catalog  # does each procedure still answer its own requests
oska conform           # does the runtime still behave the way the standard says
```

The middle one is the newer half and the one a catalog change is most likely to
break. A skill may carry an `evaluation.json` beside it holding the requests
that must reach it and the requests that must not; `evaluate catalog` runs every
such set through the same ranking the runtime uses and exits non-zero on any
failing case. A new procedure whose triggers overlap an existing one does not
announce itself in `validate` and does not show up in a scenario unless somebody
wrote one for it: it shows up here, as a case that used to reach one procedure
and now reaches another. The format and the authoring command are in
[skill-contract.md](skill-contract.md#the-evaluation-set-beside-it).

It ranks by keyword overlap and needs no provider, so it is offline and
identical on every machine, which is what makes it a build step. A deployment
that ranks by embedding should also run the sets against its own provider before
publishing, because that is the ranking its conversations will get.

The same sets run again at the quality gate: a proposal that revises a skill and
cannot answer that skill's own cases is refused. A skill with no set is not
refused, and both commands name the procedures that carry none, so the absence
is visible rather than silent.

## The service-level numbers

A suite says whether the runtime behaves correctly on cases somebody wrote down.
It says nothing about how a deployment is actually serving, and that is a
different question with a different command:

```sh
oska slo report
oska slo report --since 7 --json
```

Six numbers, read back from records the deployment keeps anyway: the median and
the tail of how long a turn takes, how often a deterministic template spoke
because no model could, how often the read-back after a write did not confirm
it, how often a guardrail refused, and how often a request found no procedure at
all. The last is not an error rate — it is the signal that the catalog is
missing something, and it is the number that tells an author to write a
scenario that does not exist yet.

The rule is that a number nothing recorded is reported as not computed, with the
reason, and never as zero. The two latency numbers come from traces, which are
off by default, so a deployment that has never switched tracing on is told to
switch it on rather than shown a healthy-looking zero.
[operating.md](operating.md) is how to read one.

## Writing a scenario for a new domain

1. **Get the turns right by hand first.** Run the session with `oska run`
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
export OSKA_MODEL_BASE_URL=...
export OSKA_MODEL_API_KEY=...
export OSKA_MODEL_CHAT=...
export OSKA_MODEL_EMBEDDING=...
```

Then run the turns, recording every call:

```sh
oska run --provider openai_compatible \
  --session restart --record cassettes/restart.json \
  --text "the search service in staging needs a restart, it is wedged again"
```

A session is more than one turn, and each `run` is a separate process, so the
approval is a second command with the same `--session` and the same `--record`.
The cassette is read back and added to, so the two turns end up in one file, in
order:

```sh
oska run --provider openai_compatible \
  --session restart --record cassettes/restart.json --text "yes"
```

`--record` wraps whichever provider is in force, so it also records a scripted
run; what it cannot do is record nothing, and `--record` with the provider
`none` stops with that message. The file is rewritten after every call, so a run
that dies half way keeps the steps it already had.

Then seed a scenario with it:

```sh
oska cassette to-script cassettes/restart.json --out /tmp/script.json
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
oska run --replay cassettes/restart.json --session restart --text "..."
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
