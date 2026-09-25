# 12 Conformance

Conformance is demonstrated by running a scenario suite, not by inspection. A
system claiming a level MUST publish the suite it ran and the checklist below,
filled in.

## Levels

**Level 1 — Declared.** The system declares what exists and what can be done,
and speaks from its own catalog.

Requires: `OSKA-INT-1` to `OSKA-INT-24`; `OSKA-ONT-1` to `OSKA-ONT-27`;
`OSKA-SKL-1` to `OSKA-SKL-17`; `OSKA-COG-11`; `OSKA-XC-1` to `OSKA-XC-4` and
`OSKA-XC-9`. The validator passes over the whole catalog with no issues.
Every sentence the system says about its capability is derived. The suite runs
offline against a scripted provider. Scenario categories C1, C13 and C21 pass.

**Level 2 — Governed.** Every effect on the world crosses the write path.

Requires: Level 1, plus `OSKA-GOV-1` to `OSKA-GOV-24`, `OSKA-GOV-30` to
`OSKA-GOV-34` and `OSKA-GOV-52`; `OSKA-IXN-1` to `OSKA-IXN-13` and
`OSKA-IXN-21` to `OSKA-IXN-27`; `OSKA-COG-1` to `OSKA-COG-10` and `OSKA-COG-12` to
`OSKA-COG-29`; `OSKA-XC-5` to `OSKA-XC-8`, `OSKA-XC-10` to `OSKA-XC-19`,
`OSKA-XC-23` and `OSKA-XC-35`. Scenario categories C1 to C14, C17, C18, C20
and C22 to C25 pass.

**Level 3 — Adaptive.** The catalog grows through a human-controlled
lifecycle.

Requires: Level 2, plus `OSKA-EVO-1` to `OSKA-EVO-34`, `OSKA-XC-32` and
`OSKA-GOV-54`.
Scenario categories C15 and C16 pass.

The forgetting of what was drafted from a session is required here and not at
Level 2 because the records it reaches — proposals and the change requests
composed from them — exist only where the catalog grows through the lifecycle;
a system without it has nothing drafted from a session to forget. The bounds
on drafting from a session (`OSKA-GOV-54`) are required here for the same
reason: a system that does not grow its catalog drafts nothing, and has no
draft to answer the same way or to count against a turn's budget.

A system MUST NOT claim a level whose scenario categories it does not run. A
system MAY claim Level 2 without Level 3; evolution is optional, governance is
not.

The lists name ranges rather than wildcards, and that is deliberate. A wildcard
is a claim about requirements nobody has written yet: `all OSKA-SKL-*` swept the
revision store into Level 1 on the day that store was specified, and
`all OSKA-GOV-*` swept in the identity requirements, neither of which any
scenario of the level runs. A range says what a level requires and stops there.
A requirement added to a layer therefore joins a level when someone adds it to a
list here, and not before. The ranges are inclusive at both ends and take in the
lettered identifiers that sit between them (`OSKA-COG-17a`, `OSKA-EVO-13a`);
they do not overlap each other, so what a level adds to the one below it is read
off the list rather than worked out.

### Required of a deployment, not of a level

Six groups of requirements are deliberately in no level, because they are
statements about how a system is deployed rather than about what a catalog
can be made to do.

- **Revisions.** A catalog released as a revision, signed or not
  (`OSKA-SKL-18` to `OSKA-SKL-24`, `OSKA-SKL-28` to `OSKA-SKL-36`,
  `OSKA-IXN-14` to `OSKA-IXN-20`, `OSKA-GOV-51`, `OSKA-GOV-53`), is what a
  deployment of several replicas
  needs and what a single process on its own disk does not; serving a
  directory stays legal at every level.
- **Identity.** An authenticated principal, the signed session token a
  channel may verify itself, the identity a channel may require, the subject a
  session is held to, the reviewer role and the actor a transition is recorded
  under (`OSKA-GOV-25` to `OSKA-GOV-29`, `OSKA-GOV-38` to `OSKA-GOV-44`,
  `OSKA-GOV-46` to `OSKA-GOV-48`, `OSKA-GOV-50`), are what a channel that knows
  who is speaking must then do about it; a channel that authenticates nobody
  has nothing to carry, and a deployment that requires no identity serves a
  call that carries none as its single user only where it declares itself
  single-user.
- **Storage.** The stores behind declared interfaces, the atomic claim, removal
  through the interfaces, the namespace of a shared database, and a conformance
  run that keeps to namespaces of its own (`OSKA-GOV-35` to `OSKA-GOV-37`,
  `OSKA-GOV-45`, `OSKA-XC-36`), are statements about the backend a deployment
  chooses.
- **Publication.** Handing a change request to a publisher, a branch or a pull
  request, and the actor a commit may be authored as (`OSKA-EVO-36` to
  `OSKA-EVO-40`, `OSKA-GOV-49`), is how a deployment chooses to review what its
  lifecycle produced.
- **Operations surface.** Exposing the service-level numbers for scraping and
  the checks a deployment's integration pipeline runs (`OSKA-XC-24` to
  `OSKA-XC-27`, `OSKA-XC-30`, `OSKA-XC-33`, `OSKA-XC-34`) are about where a
  deployment runs and who watches it.
- **Platforms.** Packs, the manifest, domains and tenants, what a tenant and a
  pack binding may read and change, the kit that checks and builds a pack, and
  retention and the improvement job reaching every (tenant, domain)
  (`OSKA-PLT-1` to `OSKA-PLT-30`, `OSKA-XC-31`), are how several catalogs are
  served from one runtime; a catalog served on its own is a platform of one
  domain.

No group is exercised by a scenario of either reference suite. The operational
suite serves a directory, carries no principal, keeps its state in files of its
own and publishes nothing, and the access suite is the only catalog that
declares a principal field at all. Each group is proven by unit tests about the
deployment concern itself: the revision store, the token, the storage backend,
the publisher, the exposition, the platform assembly. A level is attained by
running scenarios over one catalog, so requiring any of them at a level would
assert of a suite something no scenario of that suite supports — which is the
error the wildcards made. A platform does run the scenarios of every domain,
but it runs each suite against the engine its domain builds; what it proves
about the suites is the level of each, and what it adds is the assembly, which
no scenario reaches.

So they are required of the deployment that has them, at whatever level it
claims: a deployment serving a built revision MUST hold to the revision
requirements, a deployment whose channel authenticates its caller MUST hold to
the identity requirements, a deployment MUST hold to the storage requirements
for the backend it runs, the default included, a deployment that publishes
change requests MUST hold to the publication requirements, a deployment that
exposes its numbers or runs an integration pipeline MUST hold to the operations
requirements, and a deployment serving a manifest MUST hold to the platform
requirements. All of them are checked by the unit suite rather than by a
scenario — `tests/unit/test_revision.py`, `tests/unit/test_pinning.py` and
`tests/unit/test_signing.py`; `tests/unit/test_identity.py`,
`tests/unit/test_token.py` and `tests/unit/test_channel_identity.py`;
`tests/unit/test_storage_contract.py` and `tests/unit/test_conformance_postgres.py`;
`tests/unit/test_publishers.py`, with
the publisher tests of `tests/unit/test_channel_identity.py`;
`tests/unit/test_metrics.py`; `tests/unit/test_platform.py`,
`tests/unit/test_overlays.py` and `tests/unit/test_kit.py`, with the platform
tests of `tests/unit/test_retention.py`, `tests/unit/test_improvement.py`,
`tests/unit/test_api.py`, `tests/unit/test_channel_identity.py` and
`tests/unit/test_config.py`
— and a conformance report SHOULD state which of
them the deployment does, beside the level it claims.

### Still undecided

Evaluation sets and the catalog evaluation built on them (`OSKA-SKL-25` to
`OSKA-SKL-27`, `OSKA-EVO-35`, `OSKA-XC-28` and `OSKA-XC-29`) and the
service-level numbers (`OSKA-XC-20` to `OSKA-XC-22`) are the requirements the
three lists leave out for a different reason: not settled rather than not of a
level. They are stated in the layer documents and held to by the reference
implementation; what no level says yet is what a catalog whose skills carry no
evaluation set, or a deployment that has never turned tracing on, is entitled to
claim. They are listed under "Open items".

C19 is declared below and no level requires it yet: it is run by the access
suite alone, and requiring it at Level 2 would state something about the
operational suite that no scenario of that suite supports. It is listed under
"Open items", and becomes a Level 2 category once the operational suite runs a
scenario for it.

## Scenario format

A scenario is one session, recorded as a JSON document. The normative schema is
`spec/schemas/scenario.schema.json`.

```json
{
  "name": "02_restart_staging",
  "description": "A governed write: approval, verification, and a ledger replay.",
  "level": 2,
  "provider_script": [
    {
      "purpose": "plan",
      "expect_contains": "needs a restart",
      "response": { "procedure_id": "…", "operations": [] }
    }
  ],
  "turns": [
    {
      "user": "the search service needs a restart",
      "expect": {
        "kind": "new_task",
        "contains": ["approval", "service.restart"],
        "outcomes": ["read", "approval_pending"],
        "pending": "approval",
        "receipts": 0,
        "verification": null
      },
      "connector_behaviors": {
        "integration.container_runtime": { "service.restart": { "fail": true } }
      }
    }
  ]
}
```

Field meanings:

| Field | Meaning |
| --- | --- |
| `name` | Scenario identifier; also the session identifier for the run |
| `description` | What the scenario proves, in one sentence |
| `level` | Lowest conformance level this scenario belongs to; default 2 |
| `presentation` | Whether the engine is built with presentation on, as a domain that enables it builds its own; default `false` |
| `principal` | The identity an authenticated channel puts on every turn: `subject`, `attributes` and `authenticated`; absent means no principal |
| `provider_script` | Model responses, consumed in order, one per model call |
| `provider_script[].expect_contains` | Substring the prompt reaching this step MUST contain |
| `provider_script[].purpose` | Which call this step answers, for failure messages |
| `turns[].user` | The user's text for this turn |
| `turns[].expect.kind` | The route kind the turn MUST produce, or `guardrail` |
| `turns[].expect.contains` | Fragments the reply MUST contain |
| `turns[].expect.outcomes` | The outcome kinds, in order, exactly |
| `turns[].expect.pending` | `"input"`, `"approval"` or `null` |
| `turns[].expect.receipts` | Number of receipts in the session after the turn |
| `turns[].expect.verification` | Verification status of the most recent receipt, or `null` |
| `turns[].expect.directives` | The presentation directives the turn MUST return, in order; see "The directive check" |
| `turns[].connector_behaviors` | Failures or overrides injected before the turn runs |

`guardrail` is not a route kind. It is what a runner MUST report for a turn
that a guardrail declared at the `input` point ended before the turn was
classified: there is no classification to name, because none was made, and the
scenario names the boundary instead. A runner MUST take `guardrail` from a
blocking guardrail event raised before any routing event, and MUST NOT accept a
turn that was classified and then refused as satisfying it. See `OSKA-GOV-19`.

`level` is the lowest level the scenario belongs to, and a scenario carrying no
`level` is read as level 2. A runner MUST report, per level, how many scenarios
of that level passed, and MUST report as attained the highest level whose
scenarios and those of every level below it all pass.

`connector_behaviors` is written beside `expect`, on the turn. Behaviors are
keyed by integration identifier and then by operation name, and are applied
before the turn runs; a null behavior clears the one in force. That is the
only placement, and a runner MUST NOT read the object from anywhere else.

### The directive check

A conformance scenario MAY set `presentation` and list `directives` per turn.
The runner MUST compare them in order: the same count, an equal `procedure`,
`moment` and `view` for each, and every listed field present with an equal
value. Fields that aren't listed MUST NOT be compared, because some of them,
such as a receipt identifier, differ on every run. An empty list asserts that
there are none. A turn that lists no `directives` is not checked for them.

```json
{
  "presentation": true,
  "turns": [
    {
      "user": "yes",
      "expect": {
        "kind": "approve",
        "outcomes": ["receipt"],
        "verification": "verified",
        "directives": [
          {
            "procedure": "runbook.service_restart",
            "moment": "on_verified_write",
            "view": "service_restarted",
            "fields": { "service": "search", "verification": "verified" }
          }
        ]
      }
    }
  ]
}
```

A scenario that does not set `presentation` runs an engine that returns no
directive, so a turn of it that lists a directive fails, and one that lists an
empty list passes. That is how a suite shows both halves of `OSKA-IXN-21` with
one declaration.

### The learning block

A Level 3 scenario MAY carry a `learning` block. It runs once, after the last
turn, against the session those turns built: a candidate procedure is drafted
from what the session actually executed, moved through the states the block
names, and checked where it landed.

```json
{
  "learning": {
    "draft_from_session": true,
    "provider_script": [
      { "purpose": "drafting", "expect_contains": "…", "response": { "…": "…" } }
    ],
    "expect_refused": { "to": "submitted", "by": "" },
    "transitions": [
      { "to": "submitted", "by": "s.reyes", "note": "recorded from one session" },
      { "to": "approved", "by": "s.reyes", "note": "wording and policy read" },
      { "to": "published", "by": "s.reyes", "note": "published as a new version" }
    ],
    "expect": {
      "state": "published",
      "published_id": "runbook.service_restart",
      "quality_passed": true
    }
  }
}
```

| Field | Meaning |
| --- | --- |
| `learning.draft_from_session` | Whether the candidate is drafted from the session the turns built; default `true`, and a runner MAY refuse any other source |
| `learning.provider_script` | Model responses for the drafting, spent by a provider of its own, so the wording of the candidate is scripted separately from the turns; without one the candidate is built from the recorded operations alone |
| `learning.expect_refused` | A transition that MUST be refused: `to` names the state, `by` names the actor and defaults to the empty string, the usual case |
| `learning.transitions` | The moves to perform in order, each carrying `to`, `by` and an optional `note` |
| `learning.expect.state` | The state the proposal MUST be in when the block finishes |
| `learning.expect.published_id` | The skill identifier the catalog MUST hold, published, after the block; unset means the check is not made |
| `learning.expect.quality_passed` | Whether the quality report MUST pass; unset means the check is not made |

The order is normative. `expect_refused` is attempted first, while the proposal
is still a draft, so what refuses it is the missing actor and not an edge that
leads nowhere; a refused move MUST leave the stored state unchanged, and a move
that is allowed where the scenario expects a refusal is a failure. The
transitions are then applied in order, and the first one that is refused stops
the block and is reported.

When `published_id` is set, the runner MUST check three things: the catalog
holds that identifier, it holds it as published, and the file it holds is the
one under the publication root — so a scenario cannot pass by finding the
hand-written copy the candidate was drafted from. This requires the publication
root to be one of the catalog roots of the run, which a runner MUST arrange
for a scenario carrying a learning block.

### What a runner must do

A conformant implementation MUST provide a runner that executes a directory of
scenarios and reports pass or fail per scenario. The runner MUST also assert
that the provider script was fully consumed, and that the model calls occurred
in the recorded order — a classification step must have been consumed by a
classification call, not by a planning call. A drafting script, where one is
given, MUST be fully consumed as well.

A runner MUST NOT stop at the first failure: it MUST collect every expectation
that did not hold, for every scenario, and report them all. A turn that raises
is a failure of the scenario, not of the run.

A runner MUST also check, across the whole session and on every scenario, that
every receipt the session produced is named by at least one reply, and that no
reply calls a write verified while the receipt it names says otherwise.

A runner MUST write only into storage namespaces it creates for the run, one
per scenario, and MUST remove them when each scenario ends, passed or failed
(`OSKA-XC-36`). It never touches a namespace it did not create, the default
one included, so a suite run twice over one database passes twice, and a
suite run against a database a deployment shares leaves nothing behind. The
reference runner keeps each scenario under
`conformance/<run id>/<position of its directory>/<scenario>`, nests the
(tenant, domain) of a platform inside it, and purges it through the stores.

### Three ways to run a suite

The runner is one, and it is reached three ways in the reference
implementation. Each runs the same scenarios through the same checks; they
differ in what the engine is built from.

- **One catalog.** `oska conform` runs a scenario directory against a catalog
  root and its connector bindings, and reports the level attained. This is how
  each reference suite is run on its own.
- **A platform.** `oska conform --platform <platform.yaml>` runs, for every
  domain of the manifest, the scenarios of that domain's packs against an
  engine built the way that domain builds its own — its merged catalog,
  bindings, guardrails, synonyms, host and presentation — and reports a level
  per domain, and whether every domain attains the manifest's `target_level`.
  `--domain` and `--tenant` narrow it to one.
- **A pack.** `oska pack check <pack>` validates one pack on its own, checks
  that it provides what it declares, runs its evaluation sets and its
  scenarios, and reports the level it attains against the level it claims.
  It is what a team runs before a pack is added to any platform.

A level reported by any of the three is the same statement: the highest level
whose scenarios, and those of every level below it, all pass, for the engine
that ran them.

## Required scenario categories

| Id | Category | What it must prove | Reference scenario | Level |
| --- | --- | --- | --- | --- |
| C1 | Greeting and scope | The capability list is derived from the catalog | `01_greeting` | 1 |
| C2 | Governed write | Approval on the exact payload, verified read-back, receipt; the same request again replays the ledger without writing | `02_restart_staging` | 2 |
| C3 | Second approval | `require_second_approval` needs two approvals and states how many stand | `03_restart_production` | 2 |
| C4 | Missing input then reject | A question is asked, answered, then the write is rejected and nothing runs | `04_rollback_rejected` | 2 |
| C5 | Goal replacement | A new request replaces a pending goal; no write happens | `05_goal_replacement` | 2 |
| C6 | Policy denial | A write outside policy is denied, with the rule's reason | `06_silence_denied` | 2 |
| C7 | Verification failure | An injected failed read-back yields a failed receipt, no success claim, and a blocked retry | `07_verification_failure` | 2 |
| C8 | Invalid plan | An undeclared operation is an outcome, with no approval and no ledger entry | `10_invalid_plan` | 2 |
| C9 | Cancellation | A pending approval cancelled by a new goal executes nothing; a new request gets a new approval id | `11_approval_cancelled` | 2 |
| C10 | Session limit | A write beyond `max_per_session` is denied, counting failed receipts | `12_max_per_session` | 2 |
| C11 | Approval with nothing pending | A bare approval executes nothing and answers from scope | `13_approval_with_nothing_pending` | 1 |
| C12 | Composite | Members run in order; the write inside one still stops for approval | `14_composite_triage` | 2 |
| C13 | Off scope | A request outside the catalog is declined in derived words | `08_off_scope` | 1 |
| C14 | Language | The reply is written in the language of the turn | `09_italian_reply` | 1 |
| C15 | Proposal lifecycle | A proposal cannot reach `approved` or `published` without a named actor | `15_learning_lifecycle` | 3 |
| C16 | Publication | A published skill validates, versions and routes without a restart | `15_learning_lifecycle` | 3 |
| C17 | Policy re-evaluation after approval | The state read before the write carries the operation under a heavier rule; the approval that stands no longer covers it and nothing runs | `16_denied_at_approval` | 2 |
| C18 | Illegal lifecycle move | The object is read in a state from which the planned write is not a declared move; the write is refused before the call, the reply names the state and nothing reaches the ledger | `17_illegal_move` | 2 |
| C19 | Required next step, and a move that is never suggested | An object read in a state with a `required` move is told the step it still needs; an object whose only remaining move is `offered: false` is offered nothing | `examples/access/scenarios/07_required_next_step` | 2 |
| C20 | Guardrail at the input boundary | A turn a guardrail declared at the `input` point refuses ends there, in the words of that guardrail, with no classification, no plan and no model call | `19_guardrail_block` | 2 |
| C21 | A request in a second language | The router reports the language once, the session keeps it, and a later turn decided by the rules without a model call is still answered in it | `20_other_language` | 1 |
| C22 | A human-only input is asked for | A field the procedure reserves to a person is not filled in by the model: the plan comes back with the field in `missing` and no operation, and the write carries the person's own words once they have answered | `21_human_input` | 2 |
| C23 | The subject is taken from what was shown | A read shows one record, and a later request that only points at it ("restart it") joins the focus: the identifier comes from what was shown, nothing is asked, and the write stops for approval on the exact payload | `27_subject_from_what_was_shown` | 2 |
| C24 | A directive from a verified write | A write is approved, runs and is read back, and the directive its procedure declares for a verified write is filled from the receipt alone, in the turn that verified it and no earlier | `28_directive_from_verified_write` | 2 |
| C25 | A comparative on the shown rows | Several records are shown, and the comparative that follows is handed exactly the refinements the shown rows differ on and the read accepts, and no others; a record picked by what is shown about it then stops for approval | `29_comparative_on_shown_rows` | 2 |

C17 belongs to level 2: it proves `OSKA-GOV-7`, which level 2 requires. C18,
C20 and C22 belong to level 2 for the same reason, proving `OSKA-ONT-20`,
`OSKA-GOV-19` and `OSKA-SKL-16`, and so do C23, C24 and C25, proving
`OSKA-COG-27`, `OSKA-IXN-22` and `OSKA-COG-28`. C21 belongs to level 1:
`OSKA-COG-19` is a statement about routing that holds before any write does.
C19 is required by no level; see "Open items".

C23 to C25 are run by the operational suite alone, which is the situation of
C4 to C9 and not of C19: the level lists are written about the operational
suite, which runs them, and the access suite names them among the categories it
leaves to it. Each of the three also has a unit test of its own that asserts the
requirement it proves (`tests/unit/test_subject.py`,
`tests/unit/test_presentation.py`), so none of the requirements a level takes
on for them rests on one catalog's scenario alone.

## The reference suites

The reference implementation runs two suites, over two catalogs that share no
vocabulary: `examples/ops/scenarios/` over an operational domain of services,
deployments, incidents and alerts, and `examples/access/scenarios/` over an
access management domain of accounts, groups and requests. The reference
scenario named for each category above is the one in the operational suite.

A level is attained per suite, because a level is a statement about a
catalog and the runtime that serves it, not about the package alone, and the
two suites attain different levels. The operational suite attains level 3: it
runs every category a level requires — C1 to C18 and C20 to C25 — C15 and C16
included. The access suite attains level 2: it runs C1, C2 twice, C3, C10, C12
and C19, and declares no scenario of level 3, so it claims none. A second domain that runs no evolution scenario
does not lower the level the first attains, and does not inherit it either.

A level as a runner reports it and a level as a system claims it are two
different statements, and a report MUST carry both. The runner reports the
highest level whose scenarios, and those of every level below it, all pass; it
knows nothing about categories. The claim is the stronger statement, and it
MUST name every required category the suite did not run. The access suite runs
five of the categories level 2 requires, and one it does not — C19 — so its
report states level 2 as attained by the runner and names the categories it
leaves to the operational suite. The operational suite runs them all.

Both suites are run by the same runner and the same command, each against its
own catalog and its own connector bindings; see
[reference-implementation.md](reference-implementation.md) for the two
invocations. They are also run together, as the reference platform
`examples/platform.yaml`, whose domains `operations` and `access` serve the two
packs: each domain attains the level its suite attains on its own, level 3 and
level 2, and the platform attains its `target_level` of 2 because every domain
does.

## What a catalog change runs, and what a deployment can say about itself

Two things a conformant system does that no scenario in the table above covers.
It checks, whenever the catalog changes, that every procedure still answers
the requests its author said it must and none of the requests its author said it
must not. And it can say how it is serving, in numbers a person can be woken up
for.

### Evaluation sets

A ranking threshold that is a hand-set number is a guess about a catalog, and
the guess ages as the catalog grows. The durable thing is not the number but
the worked example: the words a person would really use, next to the procedure
they meant. So a skill MAY carry its own evaluation set beside it, in
`evaluation.json` in the same version directory as `skill.md`:

```json
{
  "cases": [
    { "text": "please restart the payments service", "expects": "this", "language": "en" },
    { "text": "undo the last deployment of the api", "expects": "not this", "language": "en" }
  ]
}
```

`text` is the request. `expects` is `this` when the request must reach the skill
the set sits beside and `not this` when it must reach something else.
`language` is what the request is written in; an empty value means the author
did not say.

A case marked `this` holds when the skill is the top of the ranking and the
ranking is convincing enough for the runtime to choose it: a request that ranks
first below the floor reaches nothing in a conversation and must not pass an
evaluation either. A case marked `not this` holds when the skill is not the top
of the ranking at all. That asymmetry is deliberate. A near-miss that is the
closest match is one changed threshold away from stealing the request, so the
negative case is held to the stricter bar.

The cases that prove something are the ones that nearly match. A set written
from the skill's own triggers reaches it by construction and proves almost
nothing; a set whose negative cases are drawn from the declared triggers of the
skills that rank closest to it is what catches a new procedure taking an old
one's requests. Proposing those is a helper an author runs, and it proposes
only: what belongs in an evaluation set is a judgment about the catalog, and
some of what it proposes will be two procedures that genuinely overlap, which is
a finding rather than a case to paste.

This is not the labeled routing set of `OSKA-EVO-16`, and neither replaces the
other. That set is one document about the whole catalog, written to measure
precision over time and to contain deliberate ties; it is read by a scheduled
job and a drop in it is an alarm. An evaluation set belongs to one skill,
travels with it through review and publication, and a failing case is a build
failure rather than an alarm.

The reference catalogs carry evaluation sets for three procedures of the
operational domain and two of the access domain. Each set holds requests that
must reach its procedure, in more than one language, and at least two
adversarial near-misses drawn from a neighboring procedure.

What a set is and how it is run is required by `OSKA-SKL-25` to `OSKA-SKL-27`
in the skill layer; the quality gate running the set of the skill it is looking
at is `OSKA-EVO-35` in the evolution layer; the catalog evaluation that
computes precision and recall over every set, and fails below a configured
floor, is `OSKA-XC-28` and `OSKA-XC-29` in the cross-cutting requirements. No
level requires them yet; see "Open items".

### The service-level numbers

What a deployment alerts on is not what it logs. Five numbers say almost
everything worth being woken up for:

| Number | What it says | Read from |
| --- | --- | --- |
| Turn latency at the median and the tail | How long a turn takes | The traces |
| Fallback rate | How often the model failed and a deterministic template spoke instead | The audit trail |
| Verification failure rate | How often the read-back after a write did not confirm it | The audit trail |
| Guardrail block rate | How often a declared guardrail refused a turn | The audit trail |
| Unsupported rate | How often a request found no procedure at all | The audit trail |

The last of those is not an error rate. It is the product signal that the
catalog is missing something, and it is the one number of the five that a
healthy system can have too little of rather than too much.

Four of the five come from the audit trail, which a conformant deployment keeps
anyway. Latency comes from the traces, which are optional and off by default
(`OSKA-XC-12`). A deployment that runs with tracing off can therefore answer
four of the five, and the rule that matters is what it says about the fifth: a
number that was not recorded MUST be reported as not computed, with the reason,
and MUST NOT be reported as zero. A rate printed as zero because nothing
recorded it reads exactly like a rate that is genuinely zero, and the two mean
opposite things — one is a healthy deployment and the other is a blind one.

The same rule applies to a number that is computed and incomplete. The fallback
rate counts the turns a template spoke for because no model was configured and
the turns whose plan did not hold up, both of which are audited; it does not
count a reply the model provider failed while writing, because the reply is
written either way and nothing records which of the two wrote it. That is a gap
in what is recorded, and the way to close it is an audit event rather than a
trace. Until then the number carries a note saying what it does not cover.

These numbers are required by `OSKA-XC-20` to `OSKA-XC-22` in the cross-cutting
requirements. No level requires them yet; see "Open items".

## Checklist

Every requirement, the module that satisfies it in the reference
implementation, and the proof. Paths are relative to the repository root. A row
naming a test file names the file that carries the assertion: somewhere in it a
test asserts the requirement, rather than merely exercising the module. A row
whose proof reads `pending` names a requirement the
reference implementation holds to but no test or scenario asserts; such rows are
listed again, with the reason, under "Open items" below.

Every identifier in this table is claimed by a level, required of a deployment
under "Required of a deployment, not of a level", or left under "Still
undecided"; it is in exactly one of those three places.
`tests/unit/test_conformance_document.py` reads this document and holds it to
that, so a requirement added to a layer cannot enter or leave a level by
accident.

### Integration (L1)

| Id | Module | Proof |
| --- | --- | --- |
| OSKA-INT-1 | `src/oska/contracts/skill.py` | `tests/unit/test_catalog.py` |
| OSKA-INT-2 | `src/oska/connectors/mock.py`, `http.py`, `shell.py` | `tests/unit/test_connectors.py` |
| OSKA-INT-3 | `src/oska/planner/planner.py` | `tests/unit/test_planner.py` |
| OSKA-INT-4 | `src/oska/contracts/skill.py` | `tests/unit/test_catalog.py` |
| OSKA-INT-5 | `src/oska/catalog/checks/ontology.py`, `src/oska/ontology/graph.py` | `tests/unit/test_catalog.py`, `tests/unit/test_ontology.py` |
| OSKA-INT-6 | `src/oska/connectors/base.py` | `tests/unit/test_connectors.py` |
| OSKA-INT-7 | `src/oska/connectors/shell.py` | `tests/unit/test_connectors.py` |
| OSKA-INT-8 | `src/oska/connectors/http.py` | `tests/unit/test_connectors.py` |
| OSKA-INT-9 | `src/oska/connectors/base.py`, `src/oska/planner/planner.py` | `tests/unit/test_connectors.py` |
| OSKA-INT-10 | `src/oska/connectors/registry.py` | `tests/unit/test_connectors.py` |
| OSKA-INT-11 | `src/oska/governance/ledger.py` | `tests/unit/test_governance.py` |
| OSKA-INT-12 | `src/oska/connectors/mock.py` | `tests/unit/test_connectors.py` |
| OSKA-INT-13 | `src/oska/connectors/base.py`, `registry.py`, `src/oska/config.py` | `tests/unit/test_resilience.py` |
| OSKA-INT-14 | `src/oska/connectors/base.py`, `http.py`, `shell.py`, `mock.py` | `tests/unit/test_resilience.py` |
| OSKA-INT-15 | `src/oska/connectors/base.py` | `tests/unit/test_resilience.py` |
| OSKA-INT-16 | `src/oska/connectors/base.py` | `tests/unit/test_resilience.py` |
| OSKA-INT-17 | `src/oska/connectors/base.py` | `tests/unit/test_resilience.py` |
| OSKA-INT-18 | `src/oska/engine/execution.py`, `src/oska/engine/calls.py`, `src/oska/engine/outcomes.py` | `tests/unit/test_resilience.py`, `examples/ops/scenarios/23_capability_unavailable.json` |
| OSKA-INT-19 | `src/oska/connectors/mock.py` | `tests/unit/test_resilience.py` |
| OSKA-INT-20 | `src/oska/connectors/base.py`, `http.py`, `shell.py` | `tests/unit/test_resilience.py`, `tests/unit/test_connectors.py` |
| OSKA-INT-21 | `src/oska/connectors/base.py`, `http.py` | `tests/unit/test_resilience.py`, `tests/unit/test_connectors.py` |
| OSKA-INT-22 | `src/oska/connectors/http.py` | `tests/unit/test_connectors.py` |
| OSKA-INT-23 | `src/oska/connectors/http.py`, `src/oska/engine/settlement.py` | `tests/unit/test_connectors.py`, `tests/unit/test_resilience.py` |
| OSKA-INT-24 | `src/oska/connectors/http.py` | `tests/unit/test_connectors.py` |

### Ontology (L2)

| Id | Module | Proof |
| --- | --- | --- |
| OSKA-ONT-1 | `src/oska/contracts/skill.py`, `src/oska/ontology/graph.py` | `tests/unit/test_catalog.py`, `tests/unit/test_ontology.py` |
| OSKA-ONT-2 | `src/oska/contracts/skill.py`, `src/oska/catalog/checks/ontology.py` | `tests/unit/test_catalog.py`, `tests/unit/test_ontology.py` |
| OSKA-ONT-3 | `src/oska/catalog/checks/contract.py` | `tests/unit/test_catalog.py` |
| OSKA-ONT-4 | `src/oska/contracts/skill.py`, `src/oska/catalog/checks/ontology.py` | `tests/unit/test_catalog.py`, `tests/unit/test_ontology.py` |
| OSKA-ONT-5 | `src/oska/catalog/checks/ontology.py` | `tests/unit/test_catalog.py` |
| OSKA-ONT-6 | `src/oska/ontology/queries.py` | `tests/unit/test_ontology.py` |
| OSKA-ONT-7 | `src/oska/contracts/skill.py` | `tests/unit/test_catalog.py` |
| OSKA-ONT-8 | `src/oska/ontology/queries.py` | `tests/unit/test_ontology.py`, `tests/unit/test_cli.py` |
| OSKA-ONT-9 | `src/oska/catalog/registry.py` | `tests/unit/test_catalog.py` |
| OSKA-ONT-10 | `src/oska/catalog/checks/contract.py`, `src/oska/catalog/checks/ontology.py` | `tests/unit/test_catalog.py` |
| OSKA-ONT-11 | `src/oska/catalog/checks/ontology.py` | `tests/unit/test_catalog.py` |
| OSKA-ONT-12 | `src/oska/ontology/graph.py` | `tests/unit/test_ontology.py` |
| OSKA-ONT-13 | `src/oska/ontology/export.py` | `tests/unit/test_ontology.py` |
| OSKA-ONT-14 | `src/oska/contracts/skill.py`, `src/oska/catalog/checks/ontology.py` | `tests/unit/test_lifecycle.py` |
| OSKA-ONT-15 | `src/oska/catalog/checks/ontology.py` | `tests/unit/test_lifecycle.py` |
| OSKA-ONT-16 | `src/oska/catalog/checks/ontology.py` | `tests/unit/test_lifecycle.py` |
| OSKA-ONT-17 | `src/oska/catalog/checks/ontology.py`, `src/oska/engine/lifecycle.py` | `tests/unit/test_lifecycle.py` |
| OSKA-ONT-18 | `src/oska/catalog/checks/ontology.py` | `tests/unit/test_lifecycle.py` |
| OSKA-ONT-19 | `src/oska/catalog/checks/ontology.py` | `tests/unit/test_lifecycle.py` |
| OSKA-ONT-20 | `src/oska/engine/lifecycle.py`, `src/oska/governance/capabilities.py` | `tests/unit/test_lifecycle.py`, `examples/ops/scenarios/17_illegal_move.json` |
| OSKA-ONT-21 | `src/oska/governance/verification.py` | `tests/unit/test_lifecycle.py` |
| OSKA-ONT-22 | `src/oska/catalog/resolver.py`, `src/oska/engine/goals.py`, `src/oska/ontology/queries.py` | `tests/unit/test_lifecycle.py`, `examples/access/scenarios/07_required_next_step.json` |
| OSKA-ONT-23 | `src/oska/engine/lifecycle.py`, `src/oska/governance/verification.py`, `src/oska/contracts/skill.py` | `tests/unit/test_lifecycle.py`, `examples/ops/scenarios/22_state_of_another_object.json` |
| OSKA-ONT-24 | `src/oska/contracts/skill.py`, `src/oska/ontology/naming.py`, `src/oska/catalog/checks/presentation.py` | `tests/unit/test_presentation.py` |
| OSKA-ONT-25 | `src/oska/engine/subject.py`, `src/oska/connectors/mock.py` | `tests/unit/test_subject.py` |
| OSKA-ONT-26 | `src/oska/ontology/naming.py`, `src/oska/catalog/checks/presentation.py` | `tests/unit/test_presentation.py` |
| OSKA-ONT-27 | `src/oska/ontology/graph.py`, `src/oska/ontology/naming.py` | `tests/unit/test_ontology.py` |

### Skill (L3)

| Id | Module | Proof |
| --- | --- | --- |
| OSKA-SKL-1 | `src/oska/contracts/skill.py` | `tests/unit/test_catalog.py` |
| OSKA-SKL-2 | `src/oska/contracts/skill.py` | `tests/unit/test_catalog.py` |
| OSKA-SKL-3 | `src/oska/catalog/checks/contract.py` | `tests/unit/test_catalog.py` |
| OSKA-SKL-4 | `src/oska/catalog/checks/governance.py` | `tests/unit/test_catalog.py` |
| OSKA-SKL-5 | `src/oska/catalog/loader.py`, `src/oska/catalog/checks/contract.py`, `src/oska/catalog/checks/governance.py` | `tests/unit/test_catalog.py` |
| OSKA-SKL-6 | `src/oska/catalog/registry.py` | `tests/unit/test_catalog.py` |
| OSKA-SKL-7 | `src/oska/catalog/registry.py` | `tests/unit/test_catalog.py` |
| OSKA-SKL-8 | `src/oska/catalog/validator.py`, `src/oska/catalog/checks/common.py` | `tests/unit/test_catalog.py` |
| OSKA-SKL-9 | `src/oska/catalog/validator.py`, `src/oska/engine/factory.py` | `tests/unit/test_engine.py`, `tests/unit/test_cli.py` |
| OSKA-SKL-10 | `src/oska/catalog/registry.py` | `tests/unit/test_catalog.py` |
| OSKA-SKL-11 | `src/oska/catalog/resolver.py` | `tests/unit/test_resolver.py`, `examples/ops/scenarios/01_greeting.json` |
| OSKA-SKL-12 | `src/oska/catalog/resolver.py` | `tests/unit/test_resolver.py` |
| OSKA-SKL-13 | `src/oska/catalog/loader.py`, `registry.py` | `tests/unit/test_catalog.py` |
| OSKA-SKL-14 | `src/oska/governance/capabilities.py` | `tests/unit/test_governance.py` |
| OSKA-SKL-15 | `src/oska/contracts/skill.py` | `tests/unit/test_human_inputs.py` |
| OSKA-SKL-16 | `src/oska/planner/planner.py` | `tests/unit/test_lifecycle.py`, `examples/ops/scenarios/21_human_input.json` |
| OSKA-SKL-17 | `src/oska/planner/prompts.py`, `src/oska/planner/planner.py` | `tests/unit/test_human_inputs.py`, `tests/unit/test_planner.py` |
| OSKA-SKL-18 | `src/oska/catalog/revision.py` | `tests/unit/test_revision.py` |
| OSKA-SKL-19 | `src/oska/catalog/revision.py` | `tests/unit/test_revision.py` |
| OSKA-SKL-20 | `src/oska/catalog/revision.py`, `src/oska/catalog/validator.py` | `tests/unit/test_revision.py` |
| OSKA-SKL-21 | `src/oska/catalog/revision.py` | `tests/unit/test_revision.py` |
| OSKA-SKL-22 | `src/oska/catalog/revision.py` | `tests/unit/test_pinning.py` |
| OSKA-SKL-23 | `src/oska/catalog/registry.py` | `tests/unit/test_revision.py` |
| OSKA-SKL-24 | `src/oska/catalog/registry.py`, `src/oska/engine/factory.py` | `tests/unit/test_revision.py` |
| OSKA-SKL-25 | `src/oska/catalog/evaluation.py` | `tests/unit/test_skill_evaluation.py` |
| OSKA-SKL-26 | `src/oska/catalog/evaluation.py`, `src/oska/cli/commands_evaluate.py` | `tests/unit/test_skill_evaluation.py` |
| OSKA-SKL-27 | `src/oska/catalog/evaluation.py` | `tests/unit/test_skill_evaluation.py` |
| OSKA-SKL-28 | `src/oska/catalog/registry.py`, `src/oska/config.py` | `tests/unit/test_signing.py` |
| OSKA-SKL-29 | `src/oska/catalog/revision.py`, `src/oska/catalog/signing.py` | `tests/unit/test_signing.py`, `tests/unit/test_revision.py` |
| OSKA-SKL-30 | `src/oska/catalog/signing.py`, `src/oska/catalog/revision.py` | `tests/unit/test_signing.py` |
| OSKA-SKL-31 | `src/oska/catalog/signing.py` | `tests/unit/test_signing.py` |
| OSKA-SKL-32 | `src/oska/catalog/signing.py`, `src/oska/config.py` | `tests/unit/test_signing.py` |
| OSKA-SKL-33 | `src/oska/catalog/signing.py` | `tests/unit/test_signing.py` |
| OSKA-SKL-34 | `src/oska/catalog/revision.py`, `src/oska/catalog/signing.py` | `tests/unit/test_signing.py` |
| OSKA-SKL-35 | `src/oska/catalog/loader.py`, `src/oska/catalog/registry.py` | `tests/unit/test_signing.py` |
| OSKA-SKL-36 | `src/oska/catalog/registry.py` | `tests/unit/test_revision.py` |

### Governance (L4)

| Id | Module | Proof |
| --- | --- | --- |
| OSKA-GOV-1 | `src/oska/governance/capabilities.py` | `tests/unit/test_governance.py`, `examples/ops/scenarios/10_invalid_plan.json` |
| OSKA-GOV-2 | `src/oska/governance/policy_engine.py` | `tests/unit/test_governance.py` |
| OSKA-GOV-3 | `src/oska/governance/policy_engine.py` | `tests/unit/test_governance.py` |
| OSKA-GOV-4 | `src/oska/governance/approval.py`, `src/oska/engine/execution.py`, `src/oska/engine/approvals.py` | `examples/ops/scenarios/02_restart_staging.json` |
| OSKA-GOV-5 | `src/oska/governance/approval.py` | `tests/unit/test_governance.py`, `tests/unit/test_engine.py` |
| OSKA-GOV-6 | `src/oska/engine/approvals.py`, `src/oska/engine/execution.py` | `examples/ops/scenarios/03_restart_production.json` |
| OSKA-GOV-7 | `src/oska/engine/approvals.py`, `src/oska/engine/execution.py`, `src/oska/engine/outcomes.py` | `examples/ops/scenarios/16_denied_at_approval.json`, `tests/unit/test_engine.py` |
| OSKA-GOV-8 | `src/oska/governance/ledger.py`, `src/oska/engine/settlement.py` | `tests/unit/test_engine.py` |
| OSKA-GOV-9 | `src/oska/governance/ledger.py` | `tests/unit/test_governance.py` |
| OSKA-GOV-10 | `src/oska/governance/verification.py` | `tests/unit/test_governance.py`, `examples/ops/scenarios/07_verification_failure.json` |
| OSKA-GOV-11 | `src/oska/governance/ledger.py`, `src/oska/engine/execution.py`, `src/oska/engine/settlement.py` | `examples/ops/scenarios/02_restart_staging.json` |
| OSKA-GOV-12 | `src/oska/governance/ledger.py` | `tests/unit/test_governance.py`, `examples/ops/scenarios/12_max_per_session.json` |
| OSKA-GOV-13 | `src/oska/engine/reply.py`, `src/oska/engine/outcomes.py` | `tests/scenarios/test_scenarios.py`, `tests/unit/test_engine.py` |
| OSKA-GOV-14 | `src/oska/audit/events.py`, `src/oska/engine/execution.py`, `src/oska/engine/approvals.py`, `src/oska/engine/settlement.py` | `tests/unit/test_audit.py` |
| OSKA-GOV-15 | `src/oska/governance/guardrails.py`, `configs/guardrails.json` | `tests/unit/test_guardrails.py` |
| OSKA-GOV-16 | `src/oska/governance/guardrails.py`, `configs/guardrails.json` | `tests/unit/test_guardrails.py` |
| OSKA-GOV-17 | `src/oska/governance/guardrails.py` | `tests/unit/test_guardrails.py` |
| OSKA-GOV-18 | `src/oska/governance/guardrails.py` | `tests/unit/test_guardrails.py` |
| OSKA-GOV-19 | `src/oska/engine/boundary.py`, `src/oska/governance/guardrails.py` | `tests/unit/test_guardrails.py`, `examples/ops/scenarios/19_guardrail_block.json` |
| OSKA-GOV-20 | `src/oska/governance/guardrails.py`, `configs/guardrails.json` | `tests/unit/test_guardrails.py` |
| OSKA-GOV-21 | `src/oska/governance/ledger.py`, `src/oska/engine/settlement.py`, `src/oska/engine/calls.py` | `tests/unit/test_resilience.py` |
| OSKA-GOV-22 | `src/oska/governance/ledger.py` | `tests/unit/test_resilience.py` |
| OSKA-GOV-23 | `src/oska/governance/ledger.py` | `tests/unit/test_resilience.py` |
| OSKA-GOV-24 | `src/oska/engine/outcomes.py`, `src/oska/engine/settlement.py` | `tests/unit/test_resilience.py`, `examples/ops/scenarios/23_capability_unavailable.json` |
| OSKA-GOV-25 | `src/oska/contracts/skill.py`, `src/oska/catalog/checks/inputs.py` | `tests/unit/test_identity.py` |
| OSKA-GOV-26 | `src/oska/governance/identity.py`, `src/oska/engine/principal_inputs.py` | `tests/unit/test_identity.py` |
| OSKA-GOV-27 | `src/oska/planner/prompts.py`, `src/oska/engine/principal_inputs.py`, `src/oska/engine/collaborators.py` | `tests/unit/test_identity.py` |
| OSKA-GOV-28 | `src/oska/governance/identity.py`, `src/oska/engine/binding.py` | `tests/unit/test_identity.py` |
| OSKA-GOV-29 | `src/oska/engine/binding.py`, `src/oska/engine/state.py`, `src/oska/api/routes.py` | `tests/unit/test_identity.py`, `tests/unit/test_channel_identity.py` |
| OSKA-GOV-30 | `src/oska/engine/settlement.py`, `src/oska/governance/verification.py` | `tests/unit/test_resilience.py` |
| OSKA-GOV-31 | `src/oska/engine/settlement.py`, `src/oska/governance/ledger.py` | `tests/unit/test_resilience.py` |
| OSKA-GOV-32 | `src/oska/engine/settlement.py`, `src/oska/governance/ledger.py`, `src/oska/engine/outcomes.py` | `tests/unit/test_resilience.py` |
| OSKA-GOV-33 | `src/oska/engine/settlement.py`, `src/oska/governance/ledger.py` | `tests/unit/test_resilience.py` |
| OSKA-GOV-34 | `src/oska/engine/settlement.py`, `src/oska/governance/ledger.py` | `tests/unit/test_resilience.py` |
| OSKA-GOV-35 | `src/oska/storage/protocols.py`, `src/oska/storage/openers.py` | `tests/unit/test_storage_contract.py` |
| OSKA-GOV-36 | `src/oska/storage/protocols.py`, `src/oska/governance/ledger.py`, `src/oska/storage/postgres_ledger.py` | `tests/unit/test_storage_contract.py`, `tests/unit/test_resilience.py` |
| OSKA-GOV-37 | `src/oska/governance/retention.py`, `src/oska/storage/protocols.py` | `tests/unit/test_storage_contract.py` |
| OSKA-GOV-38 | `src/oska/governance/identity.py` | `tests/unit/test_token.py` (asserts the check goes through `hmac.compare_digest`, called with the computed MAC and the given signature; it cannot assert that the comparison runs in constant time, which is a property of `hmac.compare_digest` and not of this call site) |
| OSKA-GOV-39 | `src/oska/governance/identity.py` | `tests/unit/test_token.py` |
| OSKA-GOV-40 | `src/oska/governance/identity.py`, `src/oska/config.py` | `tests/unit/test_token.py` |
| OSKA-GOV-41 | `src/oska/governance/identity.py` | `tests/unit/test_token.py` |
| OSKA-GOV-42 | `src/oska/governance/identity.py`, `src/oska/api/identity.py` | `tests/unit/test_token.py`, `tests/unit/test_channel_identity.py` |
| OSKA-GOV-43 | `src/oska/api/identity.py` | `tests/unit/test_token.py` |
| OSKA-GOV-44 | `src/oska/api/identity.py`, `src/oska/config.py` | `tests/unit/test_token.py` |
| OSKA-GOV-45 | `src/oska/storage/postgres.py`, `postgres_ledger.py`, `postgres_sessions.py` | `tests/unit/test_storage_contract.py` |
| OSKA-GOV-46 | `src/oska/api/routes.py`, `src/oska/api/identity.py`, `src/oska/api/metrics.py`, `src/oska/config.py` | `tests/unit/test_channel_identity.py` |
| OSKA-GOV-47 | `src/oska/api/routes.py`, `src/oska/api/identity.py`, `src/oska/engine/binding.py` | `tests/unit/test_channel_identity.py`, `tests/unit/test_api.py` |
| OSKA-GOV-48 | `src/oska/api/routes.py`, `src/oska/api/identity.py`, `src/oska/contracts/proposal.py`, `src/oska/learning/lifecycle.py`, `src/oska/learning/changes.py`, `src/oska/cli/commands_propose.py` | `tests/unit/test_channel_identity.py` |
| OSKA-GOV-49 | `src/oska/learning/publishers.py`, `src/oska/cli/commands_change.py` | `tests/unit/test_channel_identity.py` |
| OSKA-GOV-50 | `src/oska/api/routes.py`, `src/oska/api/identity.py`, `src/oska/config.py` | `tests/unit/test_channel_identity.py` |
| OSKA-GOV-51 | `src/oska/governance/policy_engine.py`, `src/oska/catalog/registry.py`, `src/oska/catalog/loader.py` | `tests/unit/test_signing.py` |
| OSKA-GOV-52 | `src/oska/engine/approvals.py`, `src/oska/engine/goals.py`, `src/oska/engine/execution.py` | `tests/unit/test_presentation.py` (`test_a_write_found_unconfirmed_in_the_ledger_stops_the_rest_of_its_plan` and `test_a_write_found_confirmed_in_the_ledger_lets_its_plan_go_on` hold the ledger half: a later write never asks for an approval past an unconfirmed receipt, and does past a confirmed one) |
| OSKA-GOV-53 | `src/oska/engine/factory.py`, `src/oska/catalog/registry.py` | `tests/unit/test_revision.py` |
| OSKA-GOV-54 | `src/oska/api/routes.py`, `src/oska/engine/budget.py` | `tests/unit/test_api.py` |

### Cognition (L5)

| Id | Module | Proof |
| --- | --- | --- |
| OSKA-COG-1 | `src/oska/index/`, `src/oska/router/`, `src/oska/planner/` | `tests/unit/test_layering.py` |
| OSKA-COG-2 | `src/oska/index/discovery.py` | `tests/unit/test_index.py` |
| OSKA-COG-3 | `src/oska/index/discovery.py` | `tests/unit/test_index.py`, `examples/ops/scenarios/08_off_scope.json` |
| OSKA-COG-4 | `src/oska/index/discovery.py`, `src/oska/planner/planner.py` | `tests/unit/test_index.py`, `tests/unit/test_planner.py` |
| OSKA-COG-5 | `src/oska/router/__init__.py` | `tests/unit/test_router.py` |
| OSKA-COG-6 | `src/oska/router/fast_path.py` | `tests/unit/test_router.py` |
| OSKA-COG-7 | `src/oska/planner/planner.py` | `tests/unit/test_planner.py` |
| OSKA-COG-8 | `src/oska/planner/planner.py` | `tests/unit/test_planner.py`, `examples/ops/scenarios/10_invalid_plan.json` |
| OSKA-COG-9 | `src/oska/planner/planner.py` | `tests/unit/test_planner.py`, `examples/ops/scenarios/04_rollback_rejected.json` |
| OSKA-COG-10 | `src/oska/planner/planner.py`, `src/oska/engine/reply.py` | `tests/unit/test_planner.py` |
| OSKA-COG-11 | `src/oska/planner/scripted.py` | `tests/scenarios/test_scenarios.py` |
| OSKA-COG-12 | `src/oska/catalog/enrichment.py`, `src/oska/engine/budget.py` | `tests/unit/test_index.py`, `tests/unit/test_trace.py` |
| OSKA-COG-13 | `src/oska/catalog/enrichment.py`, `src/oska/learning/lifecycle.py`, `src/oska/cli/commands_enrich.py` | `tests/unit/test_enrichment.py`, `tests/unit/test_index.py` |
| OSKA-COG-14 | `src/oska/catalog/enrichment.py` | `tests/unit/test_enrichment.py` |
| OSKA-COG-15 | `src/oska/catalog/enrichment.py` | `tests/unit/test_enrichment.py` |
| OSKA-COG-16 | `src/oska/index/base.py` | `tests/unit/test_index.py` |
| OSKA-COG-17 | `src/oska/index/base.py`, `src/oska/contracts/common.py` | `tests/unit/test_index.py` |
| OSKA-COG-17a | `src/oska/index/base.py`, `src/oska/index/discovery.py` | `tests/unit/test_index.py` |
| OSKA-COG-18 | `src/oska/learning/lifecycle.py` | `tests/unit/test_enrichment.py` |
| OSKA-COG-19 | `src/oska/engine/engine.py`, `src/oska/engine/state.py`, `src/oska/router/base.py` | `tests/unit/test_router.py`, `examples/ops/scenarios/20_other_language.json` |
| OSKA-COG-20 | `src/oska/catalog/resolver.py`, `src/oska/engine/reply.py`, `src/oska/planner/planner.py` | `tests/unit/test_labels.py` |
| OSKA-COG-21 | `src/oska/planner/planner.py`, `src/oska/engine/reply.py` | `tests/unit/test_labels.py` |
| OSKA-COG-22 | `src/oska/planner/prompts.py`, `src/oska/router/model_router.py` | `tests/unit/test_labels.py` |
| OSKA-COG-23 | `src/oska/engine/reply.py`, `src/oska/engine/outcomes.py` | `tests/unit/test_labels.py` |
| OSKA-COG-24 | `src/oska/improvement/preferences.py`, `src/oska/engine/reply.py` | `tests/unit/test_labels.py` |
| OSKA-COG-25 | `src/oska/engine/subject.py`, `src/oska/engine/presentation.py` | `tests/unit/test_subject.py` |
| OSKA-COG-26 | `src/oska/engine/subject.py` | `tests/unit/test_subject.py` |
| OSKA-COG-27 | `src/oska/engine/subject.py`, `src/oska/planner/prompts.py` | `tests/unit/test_subject.py`, `examples/ops/scenarios/27_subject_from_what_was_shown.json` |
| OSKA-COG-28 | `src/oska/engine/subject.py`, `src/oska/planner/planner.py` | `tests/unit/test_subject.py`, `examples/ops/scenarios/29_comparative_on_shown_rows.json` |
| OSKA-COG-29 | `src/oska/engine/engine.py`, `src/oska/engine/reply.py` | `tests/unit/test_presentation.py`, `examples/ops/scenarios/08_off_scope.json` |

### Interaction (L6)

| Id | Module | Proof |
| --- | --- | --- |
| OSKA-IXN-1 | `src/oska/engine/session_store.py` | `tests/unit/test_engine.py` |
| OSKA-IXN-2 | `src/oska/engine/engine.py` | `tests/scenarios/test_scenarios.py` |
| OSKA-IXN-3 | `src/oska/engine/engine.py`, `src/oska/engine/approvals.py` | `examples/ops/scenarios/13_approval_with_nothing_pending.json` |
| OSKA-IXN-4 | `src/oska/engine/engine.py` | `examples/ops/scenarios/11_approval_cancelled.json` |
| OSKA-IXN-5 | `src/oska/engine/engine.py` | `tests/unit/test_engine.py` |
| OSKA-IXN-6 | `src/oska/engine/engine.py` | `examples/ops/scenarios/05_goal_replacement.json` |
| OSKA-IXN-7 | `src/oska/engine/engine.py`, `src/oska/engine/goals.py` | `examples/ops/scenarios/04_rollback_rejected.json` |
| OSKA-IXN-8 | `src/oska/engine/approvals.py`, `src/oska/engine/execution.py` | `examples/ops/scenarios/02_restart_staging.json`, `examples/ops/scenarios/14_composite_triage.json` |
| OSKA-IXN-9 | `src/oska/engine/engine.py`, `src/oska/engine/goals.py` | `tests/unit/test_engine.py`, `examples/ops/scenarios/14_composite_triage.json` |
| OSKA-IXN-10 | `src/oska/engine/execution.py` | `examples/ops/scenarios/02_restart_staging.json` |
| OSKA-IXN-11 | `src/oska/engine/state.py`, `src/oska/governance/ledger.py` | `tests/unit/test_engine.py` |
| OSKA-IXN-12 | `src/oska/contracts/common.py`, `src/oska/services.py`, `src/oska/cli/commands_run.py`, `src/oska/api/routes.py` | `tests/unit/test_cli.py`, `tests/unit/test_api.py` |
| OSKA-IXN-13 | `src/oska/engine/engine.py`, `src/oska/engine/reply.py` | `tests/unit/test_engine.py` |
| OSKA-IXN-14 | `src/oska/engine/binding.py`, `src/oska/engine/state.py` | `tests/unit/test_revision.py` |
| OSKA-IXN-15 | `src/oska/catalog/registry.py`, `src/oska/engine/binding.py` | `tests/unit/test_revision.py` |
| OSKA-IXN-16 | `src/oska/engine/binding.py` | `tests/unit/test_revision.py` |
| OSKA-IXN-17 | `src/oska/engine/binding.py`, `src/oska/engine/approvals.py` | `tests/unit/test_pinning.py` |
| OSKA-IXN-18 | `src/oska/engine/binding.py`, `src/oska/config.py` | `tests/unit/test_pinning.py` |
| OSKA-IXN-19 | `src/oska/engine/factory.py`, `src/oska/governance/ledger.py` | `tests/unit/test_revision.py` |
| OSKA-IXN-20 | `src/oska/engine/binding.py`, `src/oska/engine/state.py` | `tests/unit/test_pinning.py` |
| OSKA-IXN-21 | `src/oska/engine/presentation.py`, `src/oska/engine/factory.py` | `tests/unit/test_presentation.py`, `tests/unit/test_conformance.py` |
| OSKA-IXN-22 | `src/oska/engine/presentation.py` | `tests/unit/test_presentation.py`, `examples/ops/scenarios/28_directive_from_verified_write.json` |
| OSKA-IXN-23 | `src/oska/engine/presentation.py`, `src/oska/contracts/common.py` | `tests/unit/test_presentation.py` |
| OSKA-IXN-24 | `src/oska/engine/engine.py`, `src/oska/engine/presentation.py` | `tests/unit/test_presentation.py` |
| OSKA-IXN-25 | `src/oska/ontology/naming.py`, `src/oska/catalog/checks/presentation.py` | `tests/unit/test_presentation.py` |
| OSKA-IXN-26 | `src/oska/engine/presentation.py` | `tests/unit/test_presentation.py` |
| OSKA-IXN-27 | `src/oska/engine/reply.py`, `src/oska/engine/outcomes.py` | `tests/unit/test_engine.py` |

### Evolution (L7)

| Id | Module | Proof |
| --- | --- | --- |
| OSKA-EVO-1 | `src/oska/learning/proposals.py` | `tests/unit/test_learning.py` |
| OSKA-EVO-2 | `src/oska/learning/quality.py` | `tests/unit/test_learning.py` |
| OSKA-EVO-3 | `src/oska/learning/lifecycle.py` | `tests/unit/test_learning.py` |
| OSKA-EVO-4 | `src/oska/learning/lifecycle.py` | `tests/unit/test_learning.py` |
| OSKA-EVO-5 | `src/oska/learning/lifecycle.py` | `tests/unit/test_learning.py` |
| OSKA-EVO-6 | `src/oska/learning/lifecycle.py` | `tests/unit/test_learning.py` |
| OSKA-EVO-7 | `src/oska/learning/lifecycle.py` | `tests/unit/test_learning.py` |
| OSKA-EVO-8 | `src/oska/catalog/registry.py`, `src/oska/learning/lifecycle.py` | `tests/unit/test_learning.py` |
| OSKA-EVO-9 | `src/oska/learning/lifecycle.py`, `src/oska/catalog/validator.py` | `tests/unit/test_learning.py`, `examples/ops/scenarios/15_learning_lifecycle.json` |
| OSKA-EVO-10 | `src/oska/improvement/report.py`, `src/oska/improvement/job.py` | `tests/unit/test_improvement.py` |
| OSKA-EVO-11 | `src/oska/improvement/signals.py` | `tests/unit/test_improvement.py` |
| OSKA-EVO-12 | `src/oska/improvement/learned.py` | `tests/unit/test_improvement.py` |
| OSKA-EVO-13 | `src/oska/improvement/learned.py`, `examples/ops/evaluation/redactions.json` | `tests/unit/test_improvement.py` |
| OSKA-EVO-13a | `src/oska/index/base.py`, `src/oska/improvement/learned.py` | `tests/unit/test_index.py`, `tests/unit/test_improvement.py` |
| OSKA-EVO-14 | `src/oska/improvement/tuning.py` | `tests/unit/test_improvement.py` |
| OSKA-EVO-15 | `src/oska/improvement/tuning.py` | `tests/unit/test_improvement.py` |
| OSKA-EVO-16 | `src/oska/improvement/evaluation.py`, `examples/ops/evaluation/routing.json` | `tests/unit/test_improvement.py` |
| OSKA-EVO-17 | `src/oska/improvement/evaluation.py`, `src/oska/cli/commands_improve.py` | `tests/unit/test_improvement.py` |
| OSKA-EVO-18 | `src/oska/improvement/gaps.py` | `tests/unit/test_improvement.py` |
| OSKA-EVO-19 | `src/oska/improvement/job.py` | `tests/unit/test_improvement.py` |
| OSKA-EVO-20 | `src/oska/improvement/job.py` | `tests/unit/test_improvement.py` |
| OSKA-EVO-21 | `src/oska/improvement/report.py` | `tests/unit/test_improvement.py` |
| OSKA-EVO-22 | `src/oska/learning/lifecycle.py`, `src/oska/learning/changes.py` | `tests/unit/test_changes.py` |
| OSKA-EVO-23 | `src/oska/learning/changes.py` | `tests/unit/test_changes.py` |
| OSKA-EVO-24 | `src/oska/learning/changes.py`, `src/oska/cli/commands_change.py` | `tests/unit/test_changes.py` |
| OSKA-EVO-25 | `src/oska/learning/changes.py` | `tests/unit/test_changes.py` |
| OSKA-EVO-26 | `src/oska/learning/changes.py`, `src/oska/learning/lifecycle.py` | `tests/unit/test_changes.py` |
| OSKA-EVO-27 | `src/oska/api/routes.py` | `tests/unit/test_api.py` |
| OSKA-EVO-28 | `src/oska/learning/lifecycle.py` | `tests/unit/test_learning.py` |
| OSKA-EVO-29 | `src/oska/improvement/feedback.py`, `src/oska/cli/commands_improve.py` | `tests/unit/test_improvement.py` |
| OSKA-EVO-30 | `src/oska/improvement/feedback.py`, `src/oska/improvement/signals.py` | `tests/unit/test_improvement.py` |
| OSKA-EVO-31 | `src/oska/improvement/preferences.py`, `src/oska/improvement/job.py` | `tests/unit/test_improvement.py` |
| OSKA-EVO-32 | `src/oska/improvement/preferences.py`, `src/oska/engine/reply.py` | `tests/unit/test_improvement.py`, `tests/unit/test_labels.py` |
| OSKA-EVO-33 | `src/oska/improvement/preferences.py`, `src/oska/cli/commands_improve.py` | `tests/unit/test_improvement.py` |
| OSKA-EVO-34 | `src/oska/improvement/report.py`, `src/oska/improvement/job.py` | `tests/unit/test_improvement.py` |
| OSKA-EVO-35 | `src/oska/learning/quality.py` | `tests/unit/test_skill_evaluation.py` |
| OSKA-EVO-36 | `src/oska/learning/publishers.py`, `src/oska/learning/lifecycle.py` | `tests/unit/test_publishers.py` |
| OSKA-EVO-37 | `src/oska/learning/publishers.py` | `tests/unit/test_publishers.py` |
| OSKA-EVO-38 | `src/oska/learning/publishers.py` | `tests/unit/test_publishers.py` |
| OSKA-EVO-39 | `src/oska/learning/publishers.py`, `src/oska/cli/commands_change.py` | `tests/unit/test_publishers.py` |
| OSKA-EVO-40 | `src/oska/learning/lifecycle.py` | `tests/unit/test_changes.py` |

### Cross-cutting

| Id | Module | Proof |
| --- | --- | --- |
| OSKA-XC-1 | `src/oska/contracts/` | `tests/unit/test_layering.py` |
| OSKA-XC-2 | `src/oska/contracts/common.py` | `tests/unit/test_layering.py` |
| OSKA-XC-3 | `src/oska/config.py`, `configs/runtime.json` | `tests/unit/test_config.py` |
| OSKA-XC-4 | `src/oska/config.py`, `src/oska/cli/main.py` | `tests/unit/test_cli.py` |
| OSKA-XC-5 | `src/oska/audit/events.py` | `tests/unit/test_audit.py` |
| OSKA-XC-6 | `src/oska/audit/sink.py` | `tests/unit/test_audit.py` |
| OSKA-XC-7 | `src/oska/governance/ledger.py` | `tests/unit/test_governance.py` |
| OSKA-XC-8 | `src/oska/learning/lifecycle.py` | `tests/unit/test_learning.py` |
| OSKA-XC-9 | `src/oska/planner/scripted.py` | `tests/scenarios/test_scenarios.py` |
| OSKA-XC-10 | `tests/scenarios/test_scenarios.py` | `tests/scenarios/test_scenarios.py` |
| OSKA-XC-11 | `src/oska/audit/trace.py`, `src/oska/engine/engine.py`, `src/oska/engine/collaborators.py` | `tests/unit/test_trace.py` |
| OSKA-XC-12 | `src/oska/audit/trace.py`, `src/oska/config.py`, `configs/runtime.json` | `tests/unit/test_trace.py` |
| OSKA-XC-13 | `src/oska/engine/budget.py`, `src/oska/engine/engine.py` | `tests/unit/test_trace.py` |
| OSKA-XC-14 | `src/oska/engine/budget.py`, `src/oska/engine/factory.py` | `tests/unit/test_trace.py` |
| OSKA-XC-15 | `src/oska/governance/retention.py`, `src/oska/cli/commands_forget.py` | `tests/unit/test_retention.py` |
| OSKA-XC-16 | `src/oska/governance/retention.py`, `src/oska/config.py` | `tests/unit/test_retention.py` |
| OSKA-XC-17 | `src/oska/governance/retention.py`, `src/oska/audit/events.py` | `tests/unit/test_retention.py` |
| OSKA-XC-18 | `src/oska/audit/sink.py`, `src/oska/governance/retention.py` | `tests/unit/test_retention.py` |
| OSKA-XC-19 | `src/oska/engine/boundary.py`, `src/oska/governance/guardrails.py` | `tests/unit/test_retention.py` |
| OSKA-XC-20 | `src/oska/observability/metrics.py`, `src/oska/cli/commands_slo.py` | `tests/unit/test_observability.py` |
| OSKA-XC-21 | `src/oska/observability/metrics.py`, `src/oska/observability/report.py` | `tests/unit/test_observability.py` |
| OSKA-XC-22 | `src/oska/observability/metrics.py` | `tests/unit/test_observability.py` |
| OSKA-XC-23 | `src/oska/governance/retention.py`, `src/oska/cli/commands_forget.py` | `tests/unit/test_retention.py` |
| OSKA-XC-24 | `src/oska/observability/exposition.py`, `src/oska/api/metrics.py`, `src/oska/api/identity.py` | `tests/unit/test_metrics.py`, `tests/unit/test_channel_identity.py` |
| OSKA-XC-25 | `src/oska/observability/exposition.py` | `tests/unit/test_metrics.py` |
| OSKA-XC-26 | `src/oska/observability/exposition.py`, `src/oska/api/metrics.py` | `tests/unit/test_metrics.py` |
| OSKA-XC-27 | `src/oska/observability/exposition.py`, `ops/slo.md`, `ops/alerts.yml` | `tests/unit/test_metrics.py` |
| OSKA-XC-28 | `src/oska/catalog/evaluation.py` | `tests/unit/test_skill_evaluation.py` |
| OSKA-XC-29 | `src/oska/catalog/evaluation.py`, `src/oska/cli/commands_evaluate.py` | `tests/unit/test_skill_evaluation.py` |
| OSKA-XC-30 | `.github/workflows/ci.yml` | `tests/unit/test_ci_workflow.py` |
| OSKA-XC-31 | `src/oska/cli/commands_forget.py`, `src/oska/cli/commands_improve.py`, `src/oska/governance/retention.py` | `tests/unit/test_retention.py`, `tests/unit/test_improvement.py` |
| OSKA-XC-32 | `src/oska/learning/forgetting.py`, `src/oska/governance/retention.py`, `src/oska/cli/commands_forget.py` | `tests/unit/test_retention.py` (`test_forget_removes_what_was_drafted_and_keeps_an_applied_decision` holds forget, `test_expire_removes_what_was_drafted_and_keeps_an_applied_decision` holds expire) |
| OSKA-XC-33 | `src/oska/observability/exposition.py`, `src/oska/observability/metrics.py` | `tests/unit/test_metrics.py` |
| OSKA-XC-34 | `src/oska/api/metrics.py`, `src/oska/observability/metrics.py`, `src/oska/config.py` | `tests/unit/test_metrics.py` |
| OSKA-XC-35 | `src/oska/config.py`, `src/oska/platform/tenants.py` | `tests/unit/test_config.py` |
| OSKA-XC-36 | `src/oska/conformance/runner.py`, `src/oska/platform/conformance.py`, `src/oska/platform/runtime.py`, `src/oska/storage/protocols.py` | `tests/unit/test_conformance_postgres.py`, `tests/unit/test_storage_contract.py`, `tests/unit/test_ci_workflow.py` |

### Platform

| Id | Module | Proof |
| --- | --- | --- |
| OSKA-PLT-1 | `src/oska/platform/packs.py` | `tests/unit/test_platform.py` |
| OSKA-PLT-2 | `src/oska/platform/packs.py` | `tests/unit/test_platform.py` |
| OSKA-PLT-3 | `src/oska/platform/packs.py`, `src/oska/platform/versions.py` | `tests/unit/test_platform.py` |
| OSKA-PLT-4 | `src/oska/platform/packs.py` | `tests/unit/test_platform.py` |
| OSKA-PLT-5 | `src/oska/platform/manifest.py` | `tests/unit/test_platform.py` |
| OSKA-PLT-6 | `src/oska/platform/manifest.py`, `src/oska/platform/runtime.py` | `tests/unit/test_platform.py` |
| OSKA-PLT-7 | `src/oska/platform/runtime.py`, `src/oska/platform/assembly.py` | `tests/unit/test_platform.py` |
| OSKA-PLT-8 | `src/oska/platform/runtime.py`, `src/oska/api/routes.py` | `tests/unit/test_platform.py`, `tests/unit/test_api.py`, `tests/unit/test_channel_identity.py` |
| OSKA-PLT-9 | `src/oska/platform/runtime.py`, `src/oska/platform/assembly.py` | `tests/unit/test_platform.py` |
| OSKA-PLT-10 | `src/oska/platform/assembly.py` | `tests/unit/test_platform.py` |
| OSKA-PLT-11 | `src/oska/platform/assembly.py` | `tests/unit/test_platform.py` |
| OSKA-PLT-12 | `src/oska/platform/host.py` | `tests/unit/test_platform.py`, `tests/unit/test_api.py` |
| OSKA-PLT-13 | `src/oska/platform/tenants.py` | `tests/unit/test_platform.py`, `tests/unit/test_overlays.py` |
| OSKA-PLT-14 | `src/oska/api/routes.py`, `src/oska/platform/tenants.py`, `src/oska/config.py` | `tests/unit/test_api.py`, `tests/unit/test_platform.py`, `tests/unit/test_channel_identity.py` |
| OSKA-PLT-15 | `src/oska/platform/conformance.py`, `src/oska/cli/commands_serve.py` | `tests/unit/test_cli.py` |
| OSKA-PLT-16 | `src/oska/platform/runtime.py` | `tests/unit/test_platform.py` |
| OSKA-PLT-17 | `src/oska/api/routes.py`, `src/oska/cli/options.py` | `tests/unit/test_api.py`, `tests/unit/test_cli.py` |
| OSKA-PLT-18 | `src/oska/platform/runtime.py`, `src/oska/platform/assembly.py` | `tests/unit/test_platform.py` |
| OSKA-PLT-19 | `src/oska/platform/runtime.py`, `src/oska/engine/reply.py` | `tests/unit/test_platform.py` |
| OSKA-PLT-20 | `src/oska/platform/runtime.py`, `src/oska/storage/protocols.py` | `tests/unit/test_platform.py` |
| OSKA-PLT-21 | `src/oska/platform/tenants.py`, `src/oska/platform/assembly.py` | `tests/unit/test_overlays.py` |
| OSKA-PLT-22 | `src/oska/platform/assembly.py`, `src/oska/connectors/registry.py`, `src/oska/contracts/platform.py` | `tests/unit/test_overlays.py` |
| OSKA-PLT-23 | `src/oska/connectors/registry.py`, `src/oska/platform/assembly.py`, `src/oska/kit/check.py` | `tests/unit/test_overlays.py`, `tests/unit/test_kit.py` |
| OSKA-PLT-24 | `src/oska/platform/tenants.py` | `tests/unit/test_overlays.py` |
| OSKA-PLT-25 | `src/oska/platform/runtime.py` | `tests/unit/test_platform.py` |
| OSKA-PLT-26 | `src/oska/platform/runtime.py`, `src/oska/api/app.py` | `tests/unit/test_platform.py` |
| OSKA-PLT-27 | `src/oska/kit/check.py`, `src/oska/kit/build.py`, `src/oska/cli/commands_pack.py` | `tests/unit/test_kit.py` |
| OSKA-PLT-28 | `src/oska/api/routes.py`, `src/oska/api/identity.py` | `tests/unit/test_channel_identity.py` |
| OSKA-PLT-29 | `src/oska/api/routes.py` | `tests/unit/test_api.py` |
| OSKA-PLT-30 | `src/oska/platform/tenants.py`, `src/oska/config.py` | `tests/unit/test_config.py` |

## Open items

Earlier pending rows are closed: `OSKA-SKL-22`, `OSKA-IXN-17` and `OSKA-IXN-18`
in `tests/unit/test_pinning.py`, which interrupts a build part-way and reads
the store, and then asks for an approval, publishes a second revision while it
waits and answers it — once with session pinning on and once with it off;
`OSKA-COG-22` in `tests/unit/test_labels.py`, which answers the same request as
a turn in two languages and reads the prompts the router and the planner were
handed; `OSKA-XC-19` in `tests/unit/test_retention.py`, which sends a turn
carrying a credential and finds the redacted wording, and nothing else, in the
audit trail and in the session store.

The five rows that read `pending` are closed the same way, each by a test
named on its checklist row: `OSKA-ONT-27` in `tests/unit/test_ontology.py`,
which reads `noun` and `plural` off an entity node — declared and defaulted —
and off the planner prompt that names an entity by them, the path the runtime
reads the ontology through; `OSKA-SKL-33` in `tests/unit/test_signing.py`,
which registers a second signer under another name and has a revision signed
and verified through it, the same way the built-in signer is; `OSKA-IXN-23` in
`tests/unit/test_presentation.py`, which runs the same turns with presentation
on and off and finds the reply, the outcomes and what is pending identical,
and directives only when it is on; `OSKA-XC-30` in
`tests/unit/test_ci_workflow.py`, which reads `.github/workflows/ci.yml` and
finds the lint, the suite, the validation, the evaluation gate and the
conformance run, each over both reference catalogs; and `OSKA-PLT-4` in
`tests/unit/test_platform.py`, which gives `check_requires` a pack whose
`requires` names a runtime range the installed runtime falls outside of.
`OSKA-ONT-27` now joins Level 1 and `OSKA-IXN-23` joins Level 2, as the level
lists said they would once proven.

The clauses that had a row but no assertion are closed too: that a
client-supplied context never overrides the focus (`OSKA-COG-27`, in
`tests/unit/test_subject.py`); that one skill identifier declared by two packs
of one domain refuses the platform (`OSKA-PLT-7`, refused in
`src/oska/platform/assembly.py` and asserted in `tests/unit/test_platform.py`);
that two engines never share a connector (`OSKA-PLT-10`, in
`tests/unit/test_platform.py`); and that a publisher never forces and the
serving process never merges (`OSKA-EVO-38`, in `tests/unit/test_publishers.py`,
against a temporary repository). What `OSKA-GOV-38`'s test proves and does not
prove is stated on its own row, since the gap there is a limit of what a test
can observe rather than something left to assert.

Two things are still required by no level, and are open in the sense that a
decision about them is owed. The revision store and the identity requirements
were open in the same sense until the level lists were written out in full; they
are now settled the other way, under "Required of a deployment, not of a level",
with the storage, publication, operations-surface and platform requirements,
and are not repeated here. Evaluation sets and the catalog evaluation over them
(`OSKA-SKL-25` to `OSKA-SKL-27`, `OSKA-EVO-35`, `OSKA-XC-28`, `OSKA-XC-29`) and
the service-level numbers (`OSKA-XC-20` to `OSKA-XC-22`) are stated in the layer
documents, which is what the level lists are written about, so nothing
structural stands in the way.
What is missing is a decision: a level that required `OSKA-SKL-26` would also
have to say what a catalog whose skills carry no evaluation set is entitled to
claim, and a level that required `OSKA-XC-20` would have to say the same about a
deployment that has never turned tracing on. Both belong in a level once that is
settled.

One further gap is recorded here because it affects a claim rather than a
requirement. Category C19 is run by the access suite alone, because that is the
only reference catalog whose entities declare a `required` move and a move
marked `offered: false`. `OSKA-ONT-22` is proven, in
`tests/unit/test_lifecycle.py` and in the access scenario; what is missing is a
second catalog declaring the flags, so C19 is required by no level. An
operational entity declaring them, and a scenario over it, would move C19 to
level 2 without any other change to this document.

## Reporting conformance

A conformance report SHOULD state: the level claimed, the catalog it was run
against, the scenario directory, the counts of scenarios passed and failed per
category, and the filled checklist with any requirement marked as not
applicable justified in one sentence. A requirement MUST NOT be marked not
applicable because the system chose not to implement the layer it belongs to;
in that case the level is simply not met.
