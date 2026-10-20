# Packs and platforms

A catalog root on disk is enough for one team running one runtime. An
organization with several business areas wants one process serving all of
them, each team owning its own declarations and none of them able to see the
others'. This document is the contract that makes that possible: the pack, the
platform manifest, and the isolation rules between them. The standard is
[`spec/14-platform-blueprint.md`](../spec/14-platform-blueprint.md); this is
what the code under `src/oska/platform/` and `src/oska/contracts/platform.py`
does with it.

## The pack

A pack is a versioned, self-contained directory of declarations for one
business area: its entities, integrations, procedures and composites, the
policies beside its procedures, the bindings of its integrations, the fixtures
its mock bindings read, the scenarios that show what it attains, and the
evaluation sets that measure its routing. It is the unit a team writes,
reviews, versions and ships — everything `docs/adopting.md` walks through, laid
out so it can be deployed on its own or alongside other packs.

```
<pack root>/pack.yaml                required: the manifest
<pack root>/skills/                  required: the catalog root
<pack root>/configs/connectors.json  required: the integration bindings
<pack root>/fixtures/                optional: what the mock bindings read
<pack root>/scenarios/               optional: the pack's conformance suite
<pack root>/evaluation/              optional: routing and redaction sets
<pack root>/guardrails.json          optional: guardrail entries the pack adds
<pack root>/phrases.json             optional: phrases the pack's domains say
```

`examples/ops/` and `examples/access/` are both packs; each carries its own
`pack.yaml`.

### `pack.yaml`, annotated

```yaml
name: ops
version: 0.1.0
description: >-
  Service operations: health, logs, restarts, rollbacks, alert silences and
  incident notes over a container runtime, a deployment controller, an
  alerting system and an incident tracker.
requires:
  oska: ">=0.1.0,<1.0.0"     # a version range the runtime serving this pack must satisfy
provides:
  entities:
    - entity.alert            # every entity skill this pack declares, and no other
    - entity.deployment
    - entity.incident
    - entity.service
  procedures:
    - composite.incident_triage   # every runbook and composite, and no other
    - runbook.alert_silence
    - runbook.deployment_rollback
    - runbook.incident_note
    - runbook.log_search
    - runbook.service_health
    - runbook.service_restart
host:
  name: the operations desk       # documents who this pack is meant to answer as
  voice: brief and factual
  languages: [en, it]
level: 3                          # the conformance level this pack claims
```

`name` and pack names generally match `^[a-z][a-z0-9_-]{1,63}$`; `version` is
strict `MAJOR.MINOR.PATCH`, never a range — ranges appear only in `requires`
and in a platform's reference to a pack. A range is one or more comma-joined
clauses, each `*`, an exact version, or a comparison operator (`>=`, `<=`, `>`,
`<`, `==`) followed by a version.

`provides` is a claim that is checked, in both directions: a pack that lists an
entity or a procedure it does not declare is refused, and one that declares a
skill it does not list is refused too. `oska pack check` runs this check
before it validates anything else, so a pack whose manifest has drifted from
its `skills/` fails immediately rather than three steps later.

`depends_on` (omitted above, since `ops` and `access` need nothing from each
other) names other packs and the version range this pack accepts of each. A
platform resolves dependencies inside one domain — a pack whose dependency is
not deployed in the same domain, whose version falls outside the range, or
whose dependencies cycle, is refused, naming the packs involved.

`host` documents who this pack is meant to answer as — a note for whoever
wires it into a platform, and what `oska new pack` fills in from `--host`.
It is not, today, read by a bare `oska run --skills` or `oska serve --skills`
against the pack directly: that path answers in the catalog's own words with
no host introduction at all, the same as any catalog without a platform.
Inside a platform, a domain's own `host` in `platform.yaml` is what the
runtime actually uses — see [Host identity](#host-identity) below — and it is
not inherited from a pack's `host` automatically; a domain omitting `host`
fails validation rather than borrowing one.

`synonyms` (not shown above) maps a natural phrase onto a catalog name, so a
reply can say "your bag" where the ontology says `entity.cart`, without the
runtime keeping a word list of its own — see
[docs/presentation.md](presentation.md) for where that wording is used.

The normative schema is
[`spec/schemas/pack.schema.json`](../spec/schemas/pack.schema.json).

### Checking a pack in isolation

```sh
oska pack check examples/ops
oska pack check examples/ops/pack.yaml --json
```

`check` builds the engine the way a one-pack domain of a platform would build
it — the pack's own catalog, its own `configs/connectors.json`, its own
`scenarios/`, nothing borrowed from any other pack — validates it, runs its
evaluation sets and its scenarios, and reports the conformance level it
reaches against the level it claims. Neither reference pack binds a shell
integration, so this is what it prints:

```
ops 0.1.0: claims level 3, reaches level 3
evaluation: 18 case(s), precision 1.0
scenarios level 1: 6/6 pass
scenarios level 2: 19/19 pass
scenarios level 3: 1/1 pass
ok
```

A pack that does bind a shell integration is refused, because a check runs a
pack's bindings on this machine and a shell binding is one nobody should run
unread; `--allow-shell` is how the operator says a particular pack is trusted
enough to run anyway.

`oska pack build` writes a pack as a content-addressed tarball with a manifest
listing every file and its hash — the artifact a registry or a deployment
pipeline would move around. What is never part of a pack is left out of it: an
environment file, version-control metadata, compiled bytecode and runtime
state are skipped by name, and a symbolic link refuses the build outright,
because the archive would hold one thing and the hash would name another.
[`docs/building-a-platform.md`](building-a-platform.md) walks both from a pack
generated by the kit.

`oska pack check` reads the runtime configuration, the guardrail catalog and
the JSON schemas the way any other command does: an `OSKA_` variable when one
is set, else `configs/` and `schemas/` in the working directory, else the
defaults the installed package ships. A pack checks in any directory; see
[docs/configuration.md](configuration.md#packaged-defaults).

## The platform manifest

`platform.yaml` is one file, naming the packs an organization deploys and
grouping them into domains. It is what the organization deploys; the runtime
underneath it is the same runtime a single catalog runs on.

A pack generated with `oska new pack ... --into packs`, run from inside a
platform directory, registers itself here on the way out: it is added to
`packs` and to the default domain's `packs` list, and the manifest is
rewritten and re-validated before the command returns — no hand edit needed
before the pack is part of the platform. `--platform <path>` names the
manifest explicitly when the pack is not being written under that platform's
own `packs/` directory.

```yaml
# examples/platform.yaml
name: reference
packs:
  ops:
    path: ops                 # resolved against the directory of platform.yaml, not the cwd
    version: "0.1.0"
  access:
    path: access
    version: "0.1.0"
domains:
  - name: operations           # a scope a person talks to
    packs: [ops]                # this domain's catalog is the union of these packs, and nothing else
    host:
      name: the operations desk
      voice: brief and factual
      languages: [en, it]
    tenants: [default]
  - name: access
    packs: [access]
    host:
      name: the access desk
      voice: careful and plain
      languages: [en]
    tenants: [default]
default_domain: operations       # which domain a request naming none reaches
channels:
  cli: true
  http: true
storage:
  backend: sqlite                # or postgres; the address comes from OSKA_DATABASE_URL, never the manifest
target_level: 2                  # every domain must reach this level for the platform to
```

A relative pack `path` resolves against the directory the manifest lives in,
not the working directory, so a platform checked out anywhere still finds its
packs. The version named for a pack here must accept the version the pack
itself declares, and the name must agree — a platform that names `ops` at
`0.2.0` while `packs/ops/pack.yaml` says `0.1.0` is refused before anything
runs.

The normative schema is
[`spec/schemas/platform.schema.json`](../spec/schemas/platform.schema.json).

### Domain

A domain is a scope a person talks to: a set of packs, a host identity, and
the tenants it serves. A turn arrives in one domain and nothing of another
domain is visible to it — not its skills, not its guardrails, not its state.
`domain.packs` lists which packs make up its catalog; a skill id declared by
two packs of one domain is refused, but the same id may exist in a different
domain without conflict, because the two catalogs never meet.

### Tenant

Which tenant a call is for is resolved once, per call: in `proxy` mode, with
`identity.tenant_header` set, from that header and nothing else — the proxy
is known to own that one header, and a `tenant` entry in the attributes header
is not read once the header is named, so an attribute never overrides it.
Otherwise from the principal's `tenant` attribute (named by
`identity.tenant_claim`) when the call carries one; else `default`. No fixed
header name, such as `X-Tenant`, is ever read unconditionally — only naming it
in `identity.tenant_header` makes a proxy's header mean anything.

### Tenant overlay

A tenant is a dimension of resolution within a domain: the same domain
resolves to different state, and may resolve to different connector bindings,
for different tenants. Beside the manifest, a tenant may carry an overlay:

```
<platform root>/tenants/<tenant>/connectors.json   # changes base_url and headers of an existing binding
<platform root>/tenants/<tenant>/runtime.json       # a fragment merged over the runtime config
```

`connectors.json` changes an existing integration id's binding — never adds
one outside what the domain's packs already bind — and, of that binding, only
an HTTP binding's `base_url` and `headers`: the kind of connector, its
operations, its fixture and its resilience stay exactly what the pack
declared. An entry repeating a declared value is accepted; one changing
anything else is refused. A tenant that moves `base_url` while the pack's own
`headers` read the environment has to declare headers of its own, so a
domain's credential never follows a binding to an address the tenant chose.

`runtime.json` is merged over the runtime configuration one section at a time,
and only three sections are a tenant's to change: `reply`, `discovery.languages`
and `resilience`. A fragment naming anything else — identity, storage,
guardrails, the catalog, where state lives — is refused. Those stay the
platform's, because a tenant that could change them could lower a guarantee
for itself and, through shared infrastructure, for others. Five of those
values are ceilings rather than free settings —
`resilience.retries`, `resilience.timeout_seconds`,
`resilience.backoff_seconds`, `resilience.max_response_bytes` and
`reply.conversation_window` — a tenant may lower each below the platform's own
value and may never raise one past it.

### Environment grants

A `${NAME}` reference inside a binding a pack declares, or inside a value a
tenant's `connectors.json` changes, is checked against what the manifest
grants to read it: a domain's own `environment` for the pack's own bindings, a
tenant's `environment` for what that tenant changes in them. Nothing declared
means nothing may be read — a manifest that never mentions `environment`
grants nothing, and a binding reading any variable is refused when the
platform is assembled:

```yaml
domains:
  - name: operations
    packs: [ops]
    environment:
      names: [RUNTIME_TOKEN]      # the domain's own bindings may read exactly this
tenants:
  partner:
    environment:
      prefix: "PARTNER_"          # this tenant's changes may read anything starting with it
```

A name is granted when it is listed under `names`, or when it starts with
`prefix`. A mock binding's fixture is checked a different way: it must
resolve inside its own pack, symbolic links followed, so a fixture path
cannot be made to read a file outside it.

## Isolation

Four things never cross a boundary, and each is enforced in code rather than
left to convention.

**Catalog.** A domain's catalog is the union of its packs' `skills/` and
nothing else. A procedure of one domain is never ranked, offered, planned or
executed in another, however similar its wording.

**Connectors.** A domain's connector bindings are its packs' bindings, merged;
two engines never share one connector instance. A tenant overlay replaces a
binding by integration id on top of that merge.

**Guardrails.** A domain's guardrail catalog is the platform's catalog with
each of its packs' own `guardrails.json` entries added; an identifier declared
twice is refused, and the domain's skills are validated against the merged
result — so a guardrail claimed by a runbook always resolves, and never to two
different wordings.

**Phrases.** A domain's phrase catalog is the packaged one, then the one
`reply.phrases` names, then each of its packs' own `phrases.json`, in the
order the domain serves its packs, laid per phrase and per language. A pack
file has the shape of the phrase catalog and may only translate or reword
phrases the runtime knows; each one is checked when the platform is assembled
and by `oska pack check`, the way the catalog itself is (the placeholders of
each English phrase, approval words the approval rules accept). A phrase two
packs of one domain give differently in one language is refused with both
packs named. Another domain never hears it. See
[multilingual.md](multilingual.md#what-the-runtime-says-in-its-own-words).

**State.** Every path a runtime writes to — sessions, the write ledger, the
audit trail, traces, learned phrasings, proposals, change requests, catalog
revisions — must be relative; an absolute one is refused at startup. A
platform resolves each under `<state_dir>/<tenant>/<domain>/`, so the state of
one (tenant, domain) pair sits in its own directory and nothing has to
remember a tenant column. See [docs/configuration.md](configuration.md#storage)
for how this differs when the backend is PostgreSQL rather than SQLite.

A session is bound to the tenant and the domain of its first turn, recorded
on the session itself. A later turn for that session naming a different
tenant or a different domain is refused before it is routed, the same `403
session_refused` as any other refusal of a session, and nothing of it is
recorded in either scope — the same shape as the identity check in
[governance.md#who-may-do-what-over-http](governance.md#who-may-do-what-over-http),
because a session is one conversation with one catalog and one tenant's state
behind it, the same way it is one conversation with one person.

## Host identity

A host identity is declared, never inferred: `name`, an optional `voice`, and
the `languages` the domain expects. `docs/architecture.md`'s resolver — the
thing that turns a catalog into the sentences a runtime says about itself — is
wrapped, inside a platform, by a resolver that prefixes the declared scope with
"I am `<name>`." A greeting, a declared-scope sentence and an off-scope reply
all carry it:

```sh
$ oska run --platform examples/platform.yaml --domain access --session demo --text "hello"
Hello. I am the access desk. I can check what access a person has, add yourself
to a group, remove a person from a group, record the outcome of an access
request and settle an open access request end to end.
```

Nothing here is taken from the skills that happen to be loaded, from the
conversation, or from the caller. A pack's own `host` in `pack.yaml` is
documentation of intent, not a fallback the platform reads: `DomainSpec.host`
in `platform.yaml` is a required field, so a domain that lists a pack without
giving its own `host` fails validation rather than borrowing the pack's
silently, and a catalog run without a platform manifest at all — `oska run
--skills` or `oska serve --skills` — answers in no host's name, whatever
`pack.yaml` says.

## Channels

A platform serves the same two channels a single catalog does, with a domain
segment added.

| Route | Does |
| --- | --- |
| `GET /domains` | Every domain the platform serves, its tenants named only to a caller that may review. A deployment with no manifest returns one synthetic domain, `default` |
| `GET /domains/{domain}/capabilities` | The capability wording of one domain |
| `POST /domains/{domain}/sessions/{session_id}/turns` | Run a turn in one domain |
| `POST /sessions/{session_id}/turns` | Run a turn in the platform's default domain |
| `GET /domains/{domain}/sessions/{session_id}/receipts` | Every write receipt of one session of one domain |
| `POST /domains/{domain}/proposals/from-session/{session_id}` | Draft a candidate procedure from one session of one domain |

Every session route — a turn, its receipts, drafting a proposal from it — is
reachable both in the platform's default domain and, with `/domains/{domain}/`
in front, in any domain the platform names.

`--platform` (or `OSKA_PLATFORM`) is the manifest; `--domain` selects a domain,
defaulting to the manifest's `default_domain`; `--tenant` selects a tenant,
defaulting to `default`. `--domain` and `--tenant` are refused when no
`--platform` names a manifest for them to be a part of, and `--platform` is
refused together with `--skills` or `--connectors` — a platform names the
catalog and the bindings itself. `oska serve --platform ...` opens the HTTP
application over every domain at once; `oska run`, `oska capabilities`,
`oska conform`, `oska receipts` and the rest take `--domain` and `--tenant` to
act on one.

A catalog served without a manifest behaves as a platform of one domain,
`default` — every route and command that names no domain acts exactly as it
did before packs and platforms existed, which is what keeps
[`docs/adopting.md`](adopting.md) and the single-catalog walkthrough unchanged.

## Conformance, per domain

A platform's conformance is reported per domain, because a person talks to a
domain and not to a platform:

```sh
$ oska conform --platform examples/platform.yaml
domain operations
...
level 1: 6/6 scenarios pass
level 2: 19/19 scenarios pass
level 3: 1/1 scenarios pass
conformance level 3

domain access
...
level 1: 1/1 scenarios pass
level 2: 7/7 scenarios pass
conformance level 2

domain operations: conformance level 3
domain access: conformance level 2
platform reference: target level 2
```

`target_level` in `platform.yaml` is the floor every domain must clear for the
platform as a whole to pass; a platform whose access domain fell to level 1
would fail even though `operations` still reaches level 3, because averaging
the two would describe neither.

## Where this sits in the codebase

`oska.contracts.platform` holds `PackManifest` and `PlatformManifest`, the
typed models `pack.yaml` and `platform.yaml` validate against.
`oska.platform.packs` loads a pack and checks `provides`, `requires` and
dependency ordering; `oska.platform.manifest` loads and validates a platform
manifest; `oska.platform.assembly` builds one domain's merged catalog,
connectors and guardrails from its packs; `oska.platform.tenants` reads a
tenant's overlay and enforces which sections of the runtime configuration it
may touch; `oska.platform.host` wraps the resolver with the domain's identity;
`oska.platform.runtime.Platform` is the orchestrator — one engine per
(tenant, domain), built from these declarations and kept in a bounded cache,
evicted and rebuilt from the same declarations rather than held forever;
`oska.platform.conformance` runs a platform's scenarios domain by domain. None
of it is reachable unless `--platform` or `OSKA_PLATFORM` names a manifest.
