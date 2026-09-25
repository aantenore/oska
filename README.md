# OSKA

OSKA is a layered standard for systems in which a language model acts on real
systems on behalf of a person: ontology first, skill driven, with every effect
on the world crossing a governance boundary no configuration can lower. `oska`
is its reference implementation — a Python package that gives a team a
runtime and a kit to build its own platform from declarations, rather than a
single fixed application. The standard is in [`spec/`](spec/README.md); the
implementation is in `src/oska/`; the design notes for each package are in
[`docs/`](docs/).

A team writes packs — versioned directories of entities, integrations and
procedures for one business area — and a manifest that groups them into
domains. One runtime process serves as many packs, domains and tenants as the
manifest names, each isolated from the others, with nothing hand-written
about what any of them can say or do. `examples/ops/` and `examples/access/`
are the two reference packs; `examples/platform.yaml` serves each as its own
domain from one process.

## The problem

Operational knowledge rots. A procedure is written in a wiki, correct on the
day it is written, and then the API changes, the service is renamed, and the
runbook stays. What is automated sits in a script nobody dares run under
pressure; what is not is done by hand at three in the morning, from memory.

Handing the job to a prompt-first agent does not help. If the prompt describes
what the agent can do, the description and the system drift apart the same way
the wiki did, and the code has no way to tell a real operation from an invented
one. The risk is not that the model is wrong about a fact. It is that it is
wrong about a write, the write goes out anyway, and nobody can say afterwards
what was sent, who agreed to it, or whether it worked — or that it never
arrived and is reported as a refusal.

## The model

- **Declared operations.** An integration skill lists every operation with a
  name, a read or write flag, an input schema, an output schema and an entity
  binding. Nothing outside that list can be planned, approved or executed.
- **Ontology first.** Entities declare their fields, identifier, states and
  relations before any procedure refers to them, and how they are named and
  compared — `noun`, `display_fields`, `comparable_fields` — before anything
  shows one to a person.
- **Lifecycle as a declaration.** An entity with a lifecycle declares the
  legal moves between its states and the operation that causes each. Current
  state is read from the system of record, never inferred, so an illegal move
  stops before approval is asked for.
- **Governed write path.** A write is checked against the declarations, looked
  up in the ledger, judged by a policy, frozen on the exact payload and held
  for approval, recorded as an intent, executed, read back, and only then
  reported. No setting turns a step off.
- **A capability that is down is not a refusal.** A connector says whether the
  system answered no, whether the request was never well formed, or whether it
  never arrived — and, among those that never arrived, whether nothing was
  ever sent or the answer is merely unknown. Only the last two open a circuit
  breaker, and neither settles anything in the ledger.
- **What is shown is what may be bound.** A procedure may declare a
  presentation directive at a named moment, bound to evidence this turn
  actually produced — a completed read, a verified receipt, the record a
  person's follow-up resolved to. A binding the evidence cannot answer drops
  the whole directive; nothing here is written by a model or fed back into a
  decision.
- **A release is a trusted artifact.** Building a revision validates the
  catalog, copies what held up where nothing writes again, and names it after
  a hash of its contents; signing it lets a deployment refuse a revision that
  was not built with a specific key, or that was changed after the fact.
- **Human-controlled evolution.** A new procedure enters as a proposal, moves
  state by state with a named actor on every move, and publishing one composes
  a change request — a directory for a person to review, or a branch of a git
  repository opened for review there. The process that answers people can
  propose a change to the catalog it answers from and can never make one.
- **Isolation is structural, not conventional.** A domain's catalog, its
  connectors, its guardrails and its state are its packs' and nothing else's;
  a tenant changes only wording, languages and resilience, never identity,
  storage or the catalog. A platform of one domain behaves exactly as a single
  catalog always did.

## The layers

| Layer | Responsibility | Package |
| --- | --- | --- |
| L0 Systems | The world: systems of record, APIs, tools. Out of scope, reachable only through L1. | — |
| L1 Integration | Declared operations and the adapters that run them: mock, HTTP, shell. Failure classification, retries, response budget, circuit breaker. | `oska.connectors` |
| L2 Ontology | Entities, states, lifecycles, relations, operation bindings, naming and comparison, the shared vocabulary. | `oska.ontology` |
| L3 Skill | The skill artifact, the catalog, the validator, the capability resolver, derived routing examples, evaluation sets, revisions, signing. | `oska.catalog`, `oska.contracts.skill` |
| L4 Governance | Capability registry, policy engine, approval gate, verifier, write ledger, identity (proxy or signed token), retention, guardrail catalog, audit and traces. | `oska.governance`, `oska.audit` |
| L5 Cognition | Discovery, routing, planning, reply writing, and the provider behind them. | `oska.index`, `oska.router`, `oska.planner` |
| L6 Interaction | Session state, the turn engine, presentation directives and subject tracking, the model budget, the channels a person reaches it through. | `oska.engine`, `oska.api`, `oska.cli` |
| L7 Evolution | Proposals, quality gates, the lifecycle, change requests (directory or git branch), the self-improvement job. | `oska.learning`, `oska.improvement` |

A layer depends only on the layers below it and on the contracts, which are
owned by none of them. Three things sit beside the layers rather than inside
one. `oska.platform` assembles a process from several packs into domains and
tenants, one runtime per (tenant, domain), and behaves as a no-op when nothing
names a manifest. `oska.storage` is the persistence protocol the session store
and the write ledger are written against — SQLite by default, PostgreSQL
behind the `postgres` extra, both satisfying one contract suite, which CI
also runs against a live PostgreSQL server.
`oska.observability` reads the audit trail and the traces back and computes
the service-level numbers, exposed as text or as `GET /metrics`. `oska.kit`
is build-time only: it generates a pack or a platform from the templates
under `src/oska/templates/`, and checks or builds one pack in isolation; it is
never imported by anything that serves a turn. See
[`spec/02-layers.md`](spec/02-layers.md) for the dependency rule,
[`spec/14-platform-blueprint.md`](spec/14-platform-blueprint.md) for the
platform, and [`docs/architecture.md`](docs/architecture.md) for how the code
keeps all of it.

## Quickstart: from nothing to a checked platform

Python 3.11 or later.

```sh
uv venv && source .venv/bin/activate
pip install -e '.[dev,api]'

oska new pack widgets --entity widget --system inventory --host "Widget Desk"
oska pack check widgets              # validates, evaluates, runs its scenarios, reports its level
oska new platform storefront --pack widgets
oska conform --platform storefront/platform.yaml
```

`new pack` writes one entity, one integration, one read and one governed
write — enough to exercise the whole write path — and `pack check` passes on
it as generated. `new platform` wraps it in a manifest with one domain.
[`docs/building-a-platform.md`](docs/building-a-platform.md) continues from
here: replacing the mock connector with a real system, adding a second pack
and domain, signing revisions, moving to token identity, moving to
PostgreSQL, wiring CI and metrics — every command in it run and verified
against this repository.

## Quickstart: the two reference packs

Every command below runs from the repository root against `examples/ops/`,
which is what the defaults point at.

```sh
oska validate                 # parse and cross-check every skill
oska capabilities             # the procedures on offer, in the catalog's words
oska ontology --format mermaid
oska evaluate catalog         # run the evaluation set each skill carries
oska conform                  # run the scenario suite, report the level reached
```

`validate` reports `15 skills, no issues`. `evaluate catalog` runs the
eighteen cases the three procedures that carry a set declare, at precision 1.0
and recall 1.0, and names the ones that carry none. `conform` runs the
twenty-six scenarios under `examples/ops/scenarios/` and prints
`conformance level 3`. `examples/access/` is the second pack — access
management, sharing no vocabulary with the first — checked the same way:

```sh
oska pack check examples/access
```

Both packs served from one process, as domains of one manifest:

```sh
oska conform --platform examples/platform.yaml
oska run --platform examples/platform.yaml --domain access --session demo --text "hello"
```

With no provider configured, a turn answers what the runtime can do and
declines to plan — a working configuration, and what every command above ran
against. For a scripted turn against recorded answers, offline and
deterministic:

```sh
oska run --session walk --provider scripted \
  --script examples/ops/scenarios/02_restart_staging.json
```

For a real provider, fill `.env` from `.env.example` and export it:

```sh
set -a; . ./.env; set +a
oska run --session ops --text "how is the checkout service doing"
```

`OSKA_PROVIDER` picks the provider: `openai_compatible` or `anthropic` for one
endpoint, or `profiles` for several endpoints tried in order, which moves to the
next only on a failure worth retrying and records every fallback in the audit
trail. See [docs/configuration.md](docs/configuration.md#configs-modeljson).
`oska serve` reasons with the same provider.

`oska serve` serves the same runtime over HTTP: turns, receipts and drafting
a proposal per session (`/sessions/{id}/turns`, `/sessions/{id}/receipts`,
`/proposals/from-session/{id}`, each also reachable under
`/domains/{domain}/...` of a platform), the review queue and change requests
for a reviewer, `GET /domains`, `GET /metrics` and `GET /health`. It
authenticates nobody by default: identity arrives in headers an authenticated
proxy sets — or, with `identity.mode: token`, in a signed session token the
runtime verifies itself. It warns on stderr before it starts whenever it binds
an address other than `127.0.0.1` while `identity.required` is off: anyone who
can reach that port can then hold a conversation and ask for writes. See
[docs/governance.md#identity](docs/governance.md#identity).

## Repository layout

```
configs/          runtime.json, model.json, guardrails.json, phrases.json: thresholds, provider access, declared promises, the runtime's own sentences per language
schemas/          JSON schemas for skills, policies and proposals
ops/              slo.md and alerts.yml: the service-level targets and the alerting rules that match them
src/oska/         the packages listed in the layer table, plus platform, storage, kit and observability
examples/ops/     the reference pack: services, deployments, incidents and alerts
examples/access/  the second reference pack: accounts, groups and access requests
examples/platform.yaml   both packs served as two domains of one platform
spec/             the OSKA standard, including the platform blueprint and its schemas
docs/             how this implementation works
tests/            unit tests and the scenario suite
```

Start with [`examples/ops/README.md`](examples/ops/README.md) to see how a
pack is declared, [`docs/skill-contract.md`](docs/skill-contract.md) for the
file format, [`docs/packs-and-platforms.md`](docs/packs-and-platforms.md) for
the pack and manifest contracts, [`docs/building-a-platform.md`](docs/building-a-platform.md)
for the practitioner path, and [`docs/operating.md`](docs/operating.md) for
the jobs that belong to whoever runs an installation.

## Status

Version 0.1. Draft, private, not published anywhere. The interfaces are
stable enough to build a second pack against — one now exists — and not
stable enough to depend on. The twenty-six scenarios under
`examples/ops/scenarios/` reach conformance level 3 (Adaptive); the eight
under `examples/access/scenarios/` reach level 2 (Governed), which is every
level they declare a scenario for; served together as `examples/platform.yaml`,
each domain is checked and reported on its own. Three procedures of the
operational pack and two of the access pack carry an evaluation set, both at
precision 1.0 and recall 1.0, and the command names the ones that do not.
1516 unit and scenario tests pass, offline; 26 are skipped — the PostgreSQL
storage contract and conformance isolation tests, which run only against a
real server named by `OSKA_TEST_POSTGRES_URL`. The `postgres` job of CI runs
them against a live PostgreSQL 16 service container on every change, together
with both reference conformance suites and the platform on the relational
backend, each twice in a row over one database; a conformance run keeps to
namespaces it creates and leaves nothing behind.

## License

MIT. See [LICENSE](LICENSE).
