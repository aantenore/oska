# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Security

- The command connector refuses a payload value that would start an argument
  with `-`, so a value cannot become an option of the declared command, unless
  the vector ends the command's options with `--` first (`OSKA-INT-7`).
- Proxy identity can require a secret the proxy sends with every call
  (`identity.proxy_secret_env`); identity headers without it are refused, and
  `oska serve` warns when a public port trusts proxy headers without one
  (`OSKA-GOV-55`).

### Added

- A page at `GET /` to try the runtime in a browser: the approval card with the
  exact payload and its digest, and the receipts; `api.ui` turns it off.
  `/capabilities` lists the approval words, and `GET /demo/prompts` names what a
  replay can answer.
- An `mcp` connector binds declared operations to tools on a Model Context
  Protocol server, over stdio or streamable HTTP, with no new dependency. Each
  operation names one tool and a template of its arguments filled one value at
  a time; the server's tool list is never shown to the model. A tool error is
  `terminal`, a failure before the tool call is sent `retryable`, and a call
  with no answer `ambiguous`. A stdio server starts from a declared vector,
  never through a shell, with only `PATH` and its declared `env`. `oska pack
  check` refuses a stdio binding without `--allow-shell`, and reports an
  operation bound to a tool the server does not list (`OSKA-INT-25` to
  `OSKA-INT-29`).

- A Dockerfile and a compose file run the example catalog with PostgreSQL on
  127.0.0.1, with token and proxy identity variants; CI builds the image and
  checks its health.
- A third reference pack, `examples/data`: data pipelines, served as a third
  domain of the reference platform and conformed in CI at level 2.
- `oska run --replay` with `--session` resumes where the last replay of that
  cassette and session stopped, so a session recorded one command per turn
  replays one command per turn; `--replay-from-start` starts over.
- CI runs on Python 3.11, 3.12 and 3.13, and a package job installs the wheel
  outside the checkout and walks the newcomer path end to end.
- `SECURITY.md`, `CONTRIBUTING.md`, issue and pull request templates.
- The runtime's own sentences ship in Spanish, French and German as well, and
  the approval rules accept their yes and no (`sí`, `oui`, `ja`; `non`,
  `nein`). A pack may ship a `phrases.json` laid over the phrase catalog of
  the domains that serve it; `oska pack check` and the platform check it, and
  two packs of one domain that give a phrase differently are refused.
- With `storage.backend: postgres`, the audit trail, the proposals and the
  change requests with their packages are kept in the database as well, under
  the namespace of each (tenant, domain): every replica appends to one trail,
  reports the same service-level numbers and `/metrics`, and reviews one queue,
  and `oska forget` reaches them there (`OSKA-GOV-56`). The files backend keeps
  them where it always did.
- `tracing.exporter: otlp`, with the optional `otel` extra, sends every span to
  an OpenTelemetry collector as well as to the file, with identifiers, kinds and
  counts only and never a payload or the text of a turn (`OSKA-XC-37`); two
  turns traced at once no longer nest under each other.
- `import oska` exposes `Settings`, `load_settings`, `build_services`, `build_platform`,
  `build_engine`, `TurnResult`, `Principal`, `ScriptedModelProvider` and `ReplayProvider`,
  resolved on first use so the import stays light; `docs/embedding.md` walks a
  three-turn session in code and is run by a test.
- `oska serve --replay <cassette>` answers from a recorded session, keyed by the
  request so concurrent sessions each get their own answers, with no model and
  no key; `/health` reports the provider in force (`replay`, a provider kind, or
  `none`). The compose stack replays `restart-in-italian.json` by default
  (`OSKA_DEMO_REPLAY`), so the approval, the verified receipt and the ledger hit
  show without a key.

### Changed

- Packaging metadata: SPDX license expression, project URLs, classifiers, a
  typing marker (`py.typed`), and `oska.__version__` read from the installed
  distribution.
- A generated platform's CI workflow installs the exact `oska` version that
  generated it. The `OSKA_INSTALL` repository variable overrides the install
  source with a wheel, an index requirement or a git URL.
- A scaffolded pack puts the right article before its entity ("an item").
- A model that fails to classify a turn or to write its plan no longer fails the
  turn: routing falls back to what it does without a model, and the procedure
  ends with an `invalid_plan` outcome.
- The compose runtime files keep the default discovery and routing, so "yes"
  and "sì" approve a write there without a model.
- A positional replay that runs out stops the turn with `ReplayExhaustedError`
  instead of being answered around, wherever in the turn it runs out.

### Fixed

- An `mcp` call over HTTP ends at the deadline of its exchange, so a server that
  keeps a stream open with keep-alives no longer holds a turn until the byte
  budget runs out; a proposal moves only while its stored state is still the one
  the move was decided from, so two replicas publishing it at once compose one
  change request; and applying a change request from PostgreSQL removes the
  files it wrote when the decision cannot be recorded, so the request can be
  applied again.

## [0.1.0]

First version. The package is not published to PyPI yet; install it from a
checkout with `pip install -e '.[dev,api]'`.

### Standard

- A layered standard under `spec/`: integration, ontology, skill, governance,
  cognition, interaction and evolution, with cross-cutting rules, deployment
  topologies, a glossary and a platform blueprint.
- Numbered requirements and three conformance levels. Each level names the
  requirements it needs, and a test holds the checklist to the code.

### Runtime

- A domain-free Python runtime that executes declared skills: entity,
  integration and procedure skills, a planner held to the closure it was
  handed, and a router with a deterministic keyword fallback.
- Catalog releases as immutable revisions with a content-derived identifier,
  signed and checked file by file; a conversation is pinned to the revision it
  began on.
- Lifecycles declared as state machines, read from the system of record.
- A CLI (`oska`), an HTTP application, and Prometheus metrics with service
  targets kept next to the alerts that watch them.

### Governance

- Default-deny policy, approval of the exact payload by digest, an
  intent-first write ledger, and read-back verification before a write is
  reported.
- A write that left without an answer is settled by reading the object back,
  and is not sent again when nothing can be read.
- A guardrail catalog in which each promise says where it acts and what keeps
  it; a catalog that claims a check where nothing runs is refused at load.
- Identity in proxy or signed-token mode, a reviewer role for everything that
  changes the catalog, and conversations that can be forgotten on request or by
  a window.
- A budget per turn, an autonomy ladder for learning from traffic, and
  publication as a change request a person reviews.

### Kit and platforms

- `oska new pack` and `oska new platform` generate a pack and a platform
  repository that validate and pass their own conformance scenarios before any
  edit.
- A platform manifest names packs and domains; one process serves every domain,
  with one runtime per tenant and domain and nothing shared across them.
- `oska pack check`, `oska pack build` and `oska conform --platform`.
- Two reference domains under `examples/`: on-call operations and access
  management.

### Providers

- Chat-completions and Messages API adapters, with profiles and roles in
  `model.json`: profiles are tried in order, and every fallback is recorded in
  the audit trail without content.
- Optional embeddings, with keyword ranking as the fallback, and structured
  output that degrades in three steps.
- A recording provider that captures a live session as a cassette, a replay
  provider that runs it offline, and conversion of a cassette to a scenario
  script.

### Multilingual

- A phrase catalog keyed by language (English and Italian shipped) for every
  sentence the runtime writes itself, with a fallback phrase by phrase.
- Routing examples in other languages derived when a skill is published, never
  in a turn.

### Storage

- Stores behind an interface, with a SQLite backend and a relational backend
  (PostgreSQL, the `postgres` extra) that namespaces data per tenant and
  applies numbered migrations.
- One contract test suite run against both backends, and against a live
  PostgreSQL in CI.
