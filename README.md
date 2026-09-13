# OSKA

OSKA is a layered architectural standard for systems in which a language model
acts on real systems on behalf of a person: ontology first, skill driven, with
every effect on the world crossing a governance boundary no configuration can
lower. `opsdeck` is its reference implementation, a Python package that runs a
catalog of declared skills and nothing else. The catalog under
`examples/ops/` is the reference domain: a small estate of services with their
deployments, incidents and alerts, and the procedures an on-call engineer runs
against them. A second catalog under `examples/access/` runs the same core over
a different vocabulary. The standard is in [`spec/`](spec/README.md); the implementation
is in `src/opsdeck/`; the design notes for each package are in
[`docs/`](docs/).

## The problem

Operational knowledge rots. A procedure is written in a wiki, it is correct on
the day it is written, and then the API changes, the service is renamed, and
the runbook stays. What is automated is automated in a script that nobody dares
to run under pressure, and what is not automated is done by hand at three in
the morning, from memory.

Handing the job to a prompt-first agent does not help. If the prompt describes
what the agent can do, the description and the system drift apart the same way
the wiki did, and the code has no way to tell a real operation from an invented
one. The risk is not that the model is wrong about facts. The risk is that it
is wrong about a write, and the write goes out anyway, and nobody can say
afterwards what was sent, who agreed to it, or whether it worked.

## The model

- **Declared operations.** An integration skill lists every operation with a
  name, a read or write flag, an input schema, an output schema and an entity
  binding. Nothing outside that list can be planned, approved or executed.
- **Ontology first.** Entities declare their fields, their identifier, their
  states and their relations before any procedure refers to them. Every
  operation is bound to an entity that the catalog holds.
- **Skills as versioned artifacts.** Behavior lives in Markdown files with
  typed frontmatter, one directory per skill and one per version. The runtime
  is generic. The catalog makes it specific, and it reloads when the files on
  disk change.
- **Governed write path.** A write is checked against the declarations, looked
  up in the ledger, judged by a policy, frozen on the exact payload and held
  for approval, recorded as an intent, executed, read back, and only then
  reported. No setting turns a step off, and a procedure with no policy writes
  nothing.
- **Catalog-derived voice.** What the runtime says it can do is computed from
  the published catalog. There is no hand-written capability copy anywhere in
  the package.
- **Human-controlled evolution.** A new procedure enters as a proposal, passes
  the same validation the catalog passes, and moves state by state with a named
  actor on every move. There is no code path that approves or publishes on its
  own.

## The layers

| Layer | Responsibility | Package |
| --- | --- | --- |
| L0 Systems | The world: systems of record, APIs, tools. Out of scope, reachable only through L1. | — |
| L1 Integration | Declared operations and the adapters that run them: mock, HTTP, argv. | `opsdeck.connectors` |
| L2 Ontology | Entities, states, relations, operation bindings, the shared vocabulary. | `opsdeck.ontology` |
| L3 Skill | The skill artifact, the catalog, the validator, the capability resolver, hydration. | `opsdeck.catalog`, `opsdeck.contracts.skill` |
| L4 Governance | Capability registry, policy engine, approval gate, verifier, write ledger, audit. | `opsdeck.governance`, `opsdeck.audit` |
| L5 Cognition | Discovery, routing, planning, reply writing, and the provider behind them. | `opsdeck.index`, `opsdeck.router`, `opsdeck.planner` |
| L6 Interaction | Session state, the turn engine, the channels a person reaches it through. | `opsdeck.engine`, `opsdeck.api`, `opsdeck.cli` |
| L7 Evolution | Proposals, quality gates, the lifecycle, publication into a catalog root. | `opsdeck.learning` |

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

Every command below runs from the repository root and uses the example catalog,
because that is what the defaults point at.

```sh
opsdeck validate                 # parse and cross-check every skill
opsdeck config                   # the settings in force, as JSON
opsdeck capabilities             # the procedures on offer, in the catalog's words
opsdeck ontology                 # entities, states, relations, what acts on them
opsdeck ontology --format mermaid
opsdeck conform                  # run the scenario suite, report the level reached
```

`validate` reports `15 skills, no issues`. `conform` runs the sixteen scenarios
under `examples/ops/scenarios/` and prints `conformance level 3`.

To run a turn without a model provider, which answers what it can do and
declines to plan:

```sh
opsdeck run --session ops --text "hello"
```

To run turns against recorded answers, offline and deterministic:

```sh
opsdeck run --session walk \
  --provider scripted \
  --script examples/ops/scenarios/02_restart_staging.json
```

That reads one request per line from standard input until the input ends or you
type `/quit`.

To run against a real provider, copy `.env.example` to `.env`, fill in
`OPSDECK_MODEL_BASE_URL`, `OPSDECK_MODEL_API_KEY` and the model names, and set
`OPSDECK_PROVIDER=openai_compatible`. Nothing in the package reads `.env` on its
own, so export it first:

```sh
set -a; . ./.env; set +a
opsdeck run --session ops --text "how is the checkout service doing"
```

The HTTP application serves the same runtime on the host and port
`configs/runtime.json` names:

```sh
opsdeck serve
opsdeck serve --host 0.0.0.0 --port 8080
```

## What a turn looks like

Scenario `02_restart_staging`, replayed with the scripted provider. Three
requests, one session.

The first request asks for something that changes state. The engine routes it,
picks the procedure, plans it, runs the read, and stops at the write:

```
This changes something, so it needs your approval first.
service.restart on integration.container_runtime with {"service":"search"}
Reply yes to approve it, or no to leave it alone.
awaiting approval a92bded8-69eb-4229-a66c-2bfcdb1638f2
```

The payload shown is the payload that runs. It is frozen on a hash, so if
anything about the request changes, the approval no longer applies.

Answering `yes` re-reads the state, re-evaluates the policy against it, records
the intent, sends the call, and reads the service back:

```
search has been restarted and the read-back shows it running again under the
same name, so the receipt is verified.
receipt  integration.container_runtime  service.restart  4a9bad9f-...
```

Asking for the same restart again does not run it again. The ledger key covers
the idempotency fields the integration declares, so the second request finds the
receipt and reports it:

```
Nothing ran again: this session already restarted search, and the receipt from
that restart still stands.
```

`opsdeck receipts --session walk` prints the same receipt with its payload,
status and verification, read back from the ledger file.

## Repository layout

```
configs/          runtime.json and model.json: thresholds, paths, provider access
schemas/          JSON schemas for skills, policies and proposals
src/opsdeck/      the packages listed in the layer table
examples/ops/     the reference domain: skills, fixtures, connectors, scenarios
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
policy that turns on whether a group is privileged. It shares no vocabulary with
the operational catalog and no code with it either — the package imports
nothing from either directory. It is there to make the claim testable rather
than stated: the same runtime, pointed at another catalog and another connector
binding, greets from that catalog's own words, stops its writes for approval,
reads them back and refuses the fourth grant of a session because that catalog's
policy says three.

```sh
opsdeck capabilities --skills examples/access/skills
opsdeck conform --skills examples/access/skills \
  --scenarios examples/access/scenarios \
  --connectors examples/access/configs/connectors.json
```

Six scenarios, conformance level 2: the evolution lifecycle of level 3 is
exercised once, by the operational suite, because it is a property of the
runtime and not of a domain. `examples/access/README.md` is the shorter of the
two domain notes and the better one to copy.

## Status

Version 0.1. Draft, private, not published anywhere. The interfaces are stable
enough to build a second domain against — one now exists — and not stable
enough to depend on. The scenario suite under `examples/ops/scenarios/` reaches
OSKA conformance level 3 (Adaptive): every level 1 and level 2 scenario passes,
and the learning lifecycle scenario passes with it. The suite under
`examples/access/scenarios/` reaches level 2, which is every level it declares
a scenario for.

## License

MIT. See [LICENSE](LICENSE).
