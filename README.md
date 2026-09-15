# OSKA

OSKA is a layered architectural standard for systems in which a language model
acts on real systems on behalf of a person: ontology first, skill driven, with
every effect on the world crossing a governance boundary no configuration can
lower. `opsdeck` is its reference implementation, a Python package that runs a
catalog of declared skills and nothing else. The catalog under `examples/ops/`
is the reference domain: services with their deployments, incidents and alerts,
and the procedures an on-call engineer runs against them. The standard is in
[`spec/`](spec/README.md); the implementation is in `src/opsdeck/`; the design
notes for each package are in [`docs/`](docs/).

## The problem

Operational knowledge rots. A procedure is written in a wiki, correct on the day
it is written, and then the API changes, the service is renamed, and the runbook
stays. What is automated sits in a script nobody dares run under pressure; what
is not is done by hand at three in the morning, from memory.

Handing the job to a prompt-first agent does not help. If the prompt describes
what the agent can do, the description and the system drift apart the same way
the wiki did, and the code has no way to tell a real operation from an invented
one. The risk is not that the model is wrong about a fact. It is that it is
wrong about a write, the write goes out anyway, and nobody can say afterwards
what was sent, who agreed to it, or whether it worked.

## The model

- **Declared operations.** An integration skill lists every operation with a
  name, a read or write flag, an input schema, an output schema and an entity
  binding. Nothing outside that list can be planned, approved or executed.
- **Ontology first.** Entities declare their fields, their identifier, their
  states and their relations before any procedure refers to them. Every
  operation is bound to an entity that the catalog holds.
- **Lifecycle as a declaration.** An entity whose record has a lifecycle
  declares the legal moves between its states and the operation that causes
  each. The current state is read from the system of record, never inferred
  from the conversation, so an illegal move stops before the approval is asked
  for.
- **Skills as versioned artifacts.** Behavior lives in Markdown files with
  typed frontmatter, one directory per skill and version. The runtime is
  generic; the catalog makes it specific, and reloads when the files on disk
  change.
- **Governed write path.** A write is checked against the declarations, looked
  up in the ledger, judged by a policy, frozen on the exact payload and held
  for approval, recorded as an intent, executed, read back, and only then
  reported. No setting turns a step off, and a procedure with no policy writes
  nothing.
- **Declared promises.** A procedure names the guardrails it keeps, and one
  catalog says of each whether code enforces it at a named point or whether it
  is text handed to the model. An identifier no catalog defines is refused at
  publish.
- **Catalog-derived voice.** What the runtime says it can do is computed from
  the published catalog. There is no hand-written capability copy anywhere in
  the package, and on an entity with a lifecycle only what is legal from its
  current state is offered.
- **A bounded turn.** A turn may make a fixed number of model calls and no
  more; past that it says so and answers from what it has. No feature adds a
  call to a turn.
- **Human-controlled evolution.** A new procedure enters as a proposal, passes
  the same validation the catalog passes, and moves state by state with a named
  actor on every move. There is no code path that approves or publishes on its
  own, and the self-improvement job stops at a draft.

## The layers

| Layer | Responsibility | Package |
| --- | --- | --- |
| L0 Systems | The world: systems of record, APIs, tools. Out of scope, reachable only through L1. | — |
| L1 Integration | Declared operations and the adapters that run them: mock, HTTP, argv. | `opsdeck.connectors` |
| L2 Ontology | Entities, states, lifecycles, relations, operation bindings, the shared vocabulary. | `opsdeck.ontology` |
| L3 Skill | The skill artifact, the catalog, the validator, the capability resolver, hydration, derived routing examples. | `opsdeck.catalog`, `opsdeck.contracts.skill` |
| L4 Governance | Capability registry, policy engine, approval gate, verifier, write ledger, guardrail catalog, audit and traces. | `opsdeck.governance`, `opsdeck.audit` |
| L5 Cognition | Discovery, routing, planning, reply writing, and the provider behind them. | `opsdeck.index`, `opsdeck.router`, `opsdeck.planner` |
| L6 Interaction | Session state, the turn engine, the model budget, the channels a person reaches it through. | `opsdeck.engine`, `opsdeck.api`, `opsdeck.cli` |
| L7 Evolution | Proposals, quality gates, the lifecycle, publication into a catalog root, the self-improvement job. | `opsdeck.learning`, `opsdeck.improvement` |

A layer depends only on the layers below it and on the contracts, which are
owned by none of them. See [`spec/02-layers.md`](spec/02-layers.md) for the
rule and [`docs/architecture.md`](docs/architecture.md) for how the code keeps
it.

## Quickstart

Python 3.11 or later.

```sh
uv venv
source .venv/bin/activate
pip install -e '.[dev,api]'
```

Every command below runs from the repository root against the example catalog,
which is what the defaults point at.

```sh
opsdeck validate                 # parse and cross-check every skill
opsdeck config                   # the settings in force, as JSON
opsdeck capabilities             # the procedures on offer, in the catalog's words
opsdeck ontology                 # entities, states, relations, what acts on them
opsdeck ontology --format mermaid
opsdeck conform                  # run the scenario suite, report the level reached
```

`validate` reports `15 skills, no issues`. `conform` runs the twenty-one
scenarios under `examples/ops/scenarios/` and prints `conformance level 3`. The
second catalog is a separate run, because a suite belongs to the catalog it was
written for:

```sh
opsdeck conform --skills examples/access/skills \
  --scenarios examples/access/scenarios \
  --connectors examples/access/configs/connectors.json
```

Two commands do work that never happens during a turn. `opsdeck enrich` derives
routing examples from the declared triggers, so a catalog written in one
language is reached in another; `opsdeck improve` reads the record of what ran,
keeps what is reversible and reports the rest.

```sh
opsdeck enrich show runbook.service_restart   # declared phrases, then derived
opsdeck improve evaluate                      # route the labelled set, change nothing
```

`enrich run` needs a model provider, `improve run` an audit trail to read. See
[`docs/multilingual.md`](docs/multilingual.md) and
[`docs/improvement.md`](docs/improvement.md).

With no provider configured, `opsdeck run --session ops --text "hello"` answers
what the runtime can do and declines to plan. To run turns against recorded
answers, offline and deterministic — without `--text` it reads one request per
line until the input ends or you type `/quit`:

```sh
opsdeck run --session walk \
  --provider scripted \
  --script examples/ops/scenarios/02_restart_staging.json
```

For a real provider, fill `.env` from `.env.example` — the endpoint, the key,
the model names, `OPSDECK_PROVIDER=openai_compatible` — and export it, because
nothing in the package reads it on its own:

```sh
set -a; . ./.env; set +a
opsdeck run --session ops --text "how is the checkout service doing"
```

`opsdeck serve` serves the same runtime over HTTP, on the host and port
`configs/runtime.json` names or the ones `--host` and `--port` give it.

## What a turn looks like

Scenario `02_restart_staging`, replayed with the scripted provider: three
requests, one session. The first asks for something that changes state, so the
engine routes it, picks the procedure, plans it, runs the read, and stops at
the write:

```
This changes something, so it needs your approval first.
service.restart on integration.container_runtime with {"service":"search"}
Reply yes to approve it, or no to leave it alone.
awaiting approval a92bded8-69eb-4229-a66c-2bfcdb1638f2
```

The payload shown is the payload that runs, frozen on a hash: if anything about
the request changes, the approval no longer applies. Answering `yes` re-reads
the state, re-evaluates the policy against it, records the intent, sends the
call, and reads the service back:

```
search has been restarted and the read-back shows it running again under the
same name, so the receipt is verified.
receipt  integration.container_runtime  service.restart  4a9bad9f-...
```

Asking for the same restart again does not run it again: the ledger key covers
the idempotency fields the integration declares, so the second request finds
that receipt and reports it instead of sending anything.
`opsdeck receipts --session walk` prints it in full, read back from the ledger.

## Repository layout

```
configs/          runtime.json, model.json, guardrails.json: thresholds, provider access, declared promises
schemas/          JSON schemas for skills, policies and proposals
src/opsdeck/      the packages listed in the layer table
examples/ops/     the reference domain: skills, fixtures, connectors, scenarios, the labelled routing set
examples/access/  the second domain, same shape and a smaller catalog
spec/             the OSKA standard
docs/             how this implementation works
tests/            unit tests and the scenario suite
```

Start with [`examples/ops/README.md`](examples/ops/README.md) to see how a
domain is declared, [`docs/skill-contract.md`](docs/skill-contract.md) for the
file format, and [`docs/adopting.md`](docs/adopting.md) to put this on top of a
system of your own.

## A second domain

`examples/access/` declares access management: accounts, groups and the
requests to be put into them, with membership granted and taken away under a
policy that turns on whether a group is privileged. It shares no vocabulary and
no code with the operational catalog; the package imports nothing from either
directory. It makes the claim testable rather than stated: the same runtime,
pointed at another catalog and another connector binding, greets in that
catalog's words, stops its writes for approval, reads them back, and refuses
the fourth grant of a session because that policy says three. Its request
record carries a declared lifecycle, so a request still open is offered the
move that completes it and one already granted is offered nothing.

```sh
opsdeck capabilities --skills examples/access/skills
```

`examples/access/README.md` is the shorter of the two domain notes and the
better one to copy.

## Status

Version 0.1. Draft, private, not published anywhere. The interfaces are stable
enough to build a second domain against — one now exists — and not stable
enough to depend on. Two reference catalogs run against the same package. The
twenty-one scenarios under `examples/ops/scenarios/` reach OSKA conformance level 3
(Adaptive): every level 1 and level 2 scenario passes, and the learning
lifecycle scenario passes with it. The seven under
`examples/access/scenarios/` reach level 2, which is every level they declare a
scenario for. 498 unit and scenario tests, all of them offline and all passing.

## License

MIT. See [LICENSE](LICENSE).
