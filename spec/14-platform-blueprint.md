# 14 Platform blueprint

## Purpose

The layers describe one runtime serving one catalog. An organization rarely has
one catalog. It has several business areas, each with its own systems, its own
procedures and its own people, and it wants one place to run them from. This
document says what a platform built on OSKA is, what it is assembled from, and
which invariants hold between the parts of it that must never meet.

A platform is built by writing declarations, not code. The runtime is the same
for every platform; what changes is the packs it serves and the manifest that
groups them.

## What a platform is made of

**The runtime.** The domain-free engine, its library, its command line and its
HTTP surface. It names no business area. One process of it serves a whole
platform.

**A pack.** A versioned, self-contained directory of declarations for one
business area: the entities, integrations, procedures and composites of that
area, the policies beside its procedures, the bindings of its integrations, the
fixtures its mock bindings read, the scenarios that show what it attains, and
the evaluation sets that measure its routing. A pack is the unit a team writes,
reviews, versions and ships.

**A manifest.** One file, `platform.yaml`, naming the packs an organization
deploys and grouping them into domains. It is what the organization deploys.

**A domain.** A scope a person talks to: a set of packs, a host identity, and
the tenants it serves. A turn arrives in one domain and nothing of another
domain is visible to it.

**A tenant.** An organization, or an estate of one, served by a domain. A
tenant is a dimension of resolution: the same domain resolves to different
state, and may resolve to different bindings, for different tenants.

## The pack contract

A pack is a directory holding `pack.yaml`, which validates against
[`schemas/pack.schema.json`](schemas/pack.schema.json), and a fixed layout
beside it:

| Path | Required | What it holds |
| --- | --- | --- |
| `skills/` | yes | The catalog root: entities, integrations, runbooks, composites |
| `configs/connectors.json` | yes | The bindings of the pack's integrations, mock by default |
| `fixtures/` | no | What the mock bindings read |
| `scenarios/` | no | The conformance scenarios of the pack |
| `evaluation/` | no | The routing and redaction evaluation sets |
| `guardrails.json` | no | Guardrail entries the pack adds to the platform catalog |

`pack.yaml` carries the pack's `name` and `version`, a `description`, the
runtime versions it `requires`, the packs it `depends_on` with the version
ranges it accepts, what it `provides`, an optional default `host`, the
`synonyms` it maps onto catalog names, and the conformance `level` it claims.

`provides` is a claim that is checked. `entities` lists every entity skill of
the pack and `procedures` every runbook and composite. A pack that lists a skill
it does not declare, or declares one it does not list, is refused.

A version range is one or more comma-joined clauses, each `*`, an exact version,
or an operator (`>=`, `<=`, `>`, `<`, `==`) followed by a version.

## The manifest contract

`platform.yaml` validates against
[`schemas/platform.schema.json`](schemas/platform.schema.json). It names the
platform, maps each pack name to a `path` and the version range the platform
accepts for it, declares the `domains`, and may name a `default_domain`, the
`channels` it opens, the `storage` backend it keeps state in, and the
`target_level` every domain must attain.

A relative pack path is read from the directory of the manifest, so a platform
directory can be checked out anywhere and still find its packs.

Each domain names its packs, its host identity (`name`, `voice`, `languages`)
and its tenants, and may declare an `environment`: the variables, by name or by
prefix, that the bindings of its packs may read with `${NAME}`. The manifest's
`tenants` may declare the same for each tenant, for the changes that tenant
makes. A binding that reads a variable nobody granted it is refused when the
platform is assembled; nothing declared means nothing may be read. A mock
fixture resolves inside the pack that declares it, symbolic links followed.
Beside the manifest, a tenant may carry an overlay directory
`tenants/<tenant>/` holding:

- `connectors.json`, which may change, in every domain the tenant is served
  in, only where an HTTP binding of the same integration id points: its
  `base_url` and its `headers`. The kind of binding, its operations, its
  fixture and its resilience stay what the pack declared. A tenant that moves a
  binding whose declared headers read the environment brings headers of its
  own, so a credential of the domain never follows a binding to an address a
  tenant chose;
- `runtime.json`, a fragment merged over the runtime configuration, limited to
  what stays inside the tenant: `reply`, `discovery.languages` and
  `resilience`. The costs a call may incur and the conversation window may be
  lowered by a tenant and never raised past the platform's own values, and
  never lowered below the floor every cost has: a value below it is refused
  like one above the ceiling.

In proxy mode with a tenant header named, the tenant of a call comes from that
header only, since the proxy owns it, and a tenant attribute does not override
it; otherwise it is the principal's tenant attribute; else `default`. No
header is read as a tenant unless the configuration names it, because a header
the runtime reads without being told to is one any client can set.

Every session route, a turn, its receipts and a proposal drafted from it, is
reachable per domain, so a session of a domain other than the default one is
not left with a turn and nothing else. `GET /domains` names the domains to any
caller that may call at all, and the tenants each one serves only to a caller
that may review: who else a deployment answers is not every caller's to
learn.

## What a domain hands its runtime

Beyond its catalog, its bindings and its guardrails, a domain gives the runtime
it builds three things, and each of them is a declaration.

**Presentation.** A domain enables presentation or does not. Only a domain that
enables it has its turns return presentation directives
([08-interaction-layer.md](08-interaction-layer.md)); in every other domain a
turn returns none, whatever its procedures declare.

**Synonyms.** The words a domain's people use for the things its catalog
names are the union of the `synonyms` its packs declare, compared without
regard to case. Two packs that agree on a term add nothing to each other. Two
packs that map one term to different catalog words have made the domain
ambiguous, and that is found when the platform is assembled, not when a person
first uses the word.

**The host.** Where a domain declares a host, the reply writer is told the
host's name, voice and languages as facts, in the reply call the turn already
makes. Nothing about the host costs a call of its own.

Whether a session already belongs to a domain is asked of the session store
through its interface, so the answer is the same whichever backend keeps the
record. The check and the recording of the binding are atomic only within a
scope, and an implementation says which: the reference implementation holds
them under a lock of the serving process chosen by session id, so the first
turns of two sessions do not wait for each other while two first turns of one
session always meet, and two processes sharing one database can still bind the
first turn of one session to two domains at the same moment. A runtime or a
session store a turn is using is never closed under it by eviction, and a turn
claims its runtime in the step that finds or builds it rather than retrying
until nothing pushes it out.

## Retention on a platform

What is forgotten on request is about a person, not about a domain, and a
session id is not known in advance to belong to one (tenant, domain). Forgetting
a session or expiring the window on a platform therefore reaches every
(tenant, domain) the platform serves, unless the operator narrows it to a domain
or a tenant. The improvement job runs once per (tenant, domain), learns from
that pair's signals alone and writes only into that pair's state; what one
domain's traffic teaches never reaches another domain's routing. Retention is
specified where the rest of it is, as `OSKA-XC-31` in
[10-cross-cutting.md](10-cross-cutting.md).

## Isolation invariants

- A session never crosses domains or tenants. The domain and the tenant of its
  first turn are recorded on the session, and a turn naming the session in
  another domain, or as another tenant, anywhere on the platform, is refused
  before anything of it runs, with the refusal every other session call is
  refused with.
- A skill of one domain is never ranked, offered, planned or executed in
  another. A domain's catalog is its packs' catalogs and nothing else. Skill
  identifiers are unique within a domain; two domains may reuse one.
- A tenant's state never mixes with another's. The sessions, receipts, audit
  events, traces, proposals, change requests and revisions of one tenant in one
  domain live apart from those of every other tenant and every other domain.
- A host identity is declared, never inferred. The manifest names who the
  runtime answers as in each domain; nothing derives it from the skills, the
  conversation or the caller.
- A tenant changes only what stays inside the tenant. Identity, storage,
  guardrails, the catalog and the location of state belong to the platform.

## Requirements

- **OSKA-PLT-1** — A pack MUST be a directory holding a `pack.yaml` that
  validates against the pack schema, a `skills/` catalog root and a
  `configs/connectors.json`. A directory missing any of them MUST NOT be served
  as a pack.
- **OSKA-PLT-2** — A pack's `provides.entities` MUST list exactly the entity
  skills it declares, and `provides.procedures` exactly its runbooks and
  composites. A disagreement in either direction MUST refuse the pack and MUST
  name each skill involved.
- **OSKA-PLT-3** — A pack version MUST be `MAJOR.MINOR.PATCH`. A platform MUST
  refuse a pack whose dependency is not deployed in the same domain, whose
  dependency's version falls outside the declared range, or whose dependencies
  form a cycle, and the refusal MUST name the packs involved.
- **OSKA-PLT-4** — A pack whose `requires` range for the runtime does not
  accept the runtime serving it MUST be refused.
- **OSKA-PLT-5** — A platform manifest MUST validate against the platform
  schema. Domain names MUST be unique, a domain MUST name only declared packs,
  and a named default domain MUST be declared. Relative pack paths MUST resolve
  against the directory of the manifest.
- **OSKA-PLT-6** — The version range the manifest declares for a pack MUST
  accept the version the pack declares, and the name the manifest gives a pack
  MUST be the name the pack declares.
- **OSKA-PLT-7** — A domain's catalog MUST be the union of its packs' catalogs
  and nothing else. A skill of one domain MUST NOT be ranked, offered, planned
  or executed in another. A skill identifier declared by two packs of one
  domain MUST refuse the platform.
- **OSKA-PLT-8** — A session MUST be bound to the domain of its first turn, and
  the binding MUST be recorded on the session. A turn naming the session in
  another domain MUST be refused before it is routed, and nothing of it MUST be
  recorded in either domain. A channel's refusal of a turn in another domain or
  as another tenant MUST be indistinguishable from any other refusal of that
  session call (`OSKA-GOV-47` in [06-governance-layer.md](06-governance-layer.md)).
- **OSKA-PLT-9** — The state of each (tenant, domain) MUST be kept apart from
  every other: sessions, receipts, audit events, traces, proposals, change
  requests, preferences and catalog revisions. A configuration that would put
  any of them at one location for every (tenant, domain) MUST be refused.
- **OSKA-PLT-10** — A domain's connector bindings MUST be the merged bindings
  of its packs. An integration bound by two packs of one domain MUST refuse the
  platform. Two engines MUST NOT share a connector.
- **OSKA-PLT-11** — A domain's guardrail catalog MUST be the platform catalog
  with each of its packs' entries added. An identifier defined twice MUST refuse
  the platform, and the domain's skills MUST be checked against the merged
  catalog.
- **OSKA-PLT-12** — Each domain MUST declare its host identity in the manifest.
  The greeting, the declared scope and the off-scope reply of a domain MUST name
  that host. The runtime MUST NOT infer a host from the catalog, the
  conversation or the caller.
- **OSKA-PLT-13** — A tenant overlay binding MUST change the binding of the
  same integration id, only as OSKA-PLT-21 allows, and nothing else; a binding
  for an integration no pack of the platform declares MUST be refused. A tenant runtime fragment MUST be
  refused when it names anything other than `reply`, `discovery.languages` or
  `resilience`.
- **OSKA-PLT-14** — In proxy mode with a tenant header named, the tenant MUST
  come from that header only, and a tenant attribute MUST NOT override it;
  otherwise from the principal's tenant attribute; else `default`. A header
  the configuration does not name MUST NOT be read as a tenant. A tenant a
  domain does not serve MUST be refused, and a tenant name MUST be a single
  path segment.
- **OSKA-PLT-15** — Conformance MUST be attained and reported per domain, each
  domain running its packs' scenarios against an engine built the way that
  domain builds its own. A platform attains its `target_level` only when every
  domain does.
- **OSKA-PLT-16** — A runtime MAY keep a bounded number of (tenant, domain)
  runtimes built at once. A runtime evicted from that bound MUST be rebuilt from
  the same declarations when next asked for, and MUST resume from the state it
  left.
- **OSKA-PLT-17** — A catalog served without a manifest MUST behave as a
  platform of one domain, `default`, and MUST behave as it did before this
  document: routes and commands that name no domain act on it unchanged.
- **OSKA-PLT-18** — A domain's runtime MUST emit presentation directives only
  when the domain enables presentation. Its synonyms MUST be the union of its
  packs' synonyms, compared case-insensitively. A term that two packs of one
  domain map to different catalog words MUST be refused when the platform is
  assembled.
- **OSKA-PLT-19** — Where a domain declares a host, the reply writer MUST
  receive the host's name, voice and languages as facts, in the reply call the
  turn already makes. No additional model call is permitted for it.
- **OSKA-PLT-20** — Session-to-domain binding MUST be decided through the
  session store interface, whatever the backend. An implementation MUST state
  the scope within which the binding is atomic.
- **OSKA-PLT-21** — A tenant overlay MUST NOT change a binding's type,
  operations, fixture, resilience or any key other than `base_url` and
  `headers` of an HTTP binding; a value equal to what the pack declared is
  accepted. A tenant that moves `base_url` while the pack's headers read the
  environment MUST supply its own headers.
- **OSKA-PLT-22** — In a platform, a `${NAME}` reference MUST be admitted by the
  domain's `environment` (for pack bindings) or the tenant's `environment` (for
  tenant changes), and MUST otherwise be refused when the platform is
  assembled.
- **OSKA-PLT-23** — A mock fixture MUST resolve inside its pack, with symbolic
  links followed.
- **OSKA-PLT-24** — A tenant fragment MUST NOT exceed the platform's
  `resilience.retries`, `timeout_seconds`, `backoff_seconds`,
  `max_response_bytes` or `reply.conversation_window`.
- **OSKA-PLT-25** — A session MUST be bound to the tenant and the domain of its
  first turn across the whole platform.
- **OSKA-PLT-26** — An evicted runtime MUST be closed, and a cold build of one
  (tenant, domain) MUST NOT block another. The HTTP application MUST close its
  platform when it shuts down. Binding the first turn of one session MUST NOT
  block binding another session's. A runtime or session store in use MUST NOT
  be closed by eviction, and claiming a runtime for a turn MUST NOT retry
  without bound.
- **OSKA-PLT-27** — `pack check` MUST build the engine the way a one-pack domain
  builds it, and MUST refuse `shell` bindings, which run a command on the host,
  unless the operator allows them. `pack build` MUST leave out environment
  files, version-control metadata, caches and state, and MUST refuse a symbolic
  link.
- **OSKA-PLT-28** — `/domains` MUST name tenants only to a caller that may
  review.
- **OSKA-PLT-29** — Every session route (turn, receipts, draft) MUST be
  reachable per domain.
- **OSKA-PLT-30** — A tenant fragment value below a floor MUST be refused like
  one above the ceiling.

## Rationale

**Why packs, and why `provides` is checked.** A pack is the unit a team owns.
A pack that says it provides a procedure it does not declare fails the team
that deploys it, silently, at the moment a person asks for that procedure. A
pack that declares a procedure it does not list ships something nobody
reviewed as part of its surface. Checking both directions makes the manifest a
statement that can be read instead of trusted (PLT-2).

**Why dependencies are resolved inside a domain.** A pack that depends on
another references its entities. If the dependency is deployed only in another
domain, the reference either dangles or reaches across the domain boundary; both
break PLT-7. Resolving inside the domain is the only reading that keeps both
(PLT-3).

**Why the session is bound to a domain.** A session carries a plan, a pending
approval and a pinned revision, and each of those was composed from one
catalog. Continuing it in another domain would finish something the person
never saw, under procedures they never chose. Refusing the turn is the only
answer that keeps an approval bound to what was shown (PLT-8).

**Why state is separated by directory and not by column.** A shared store with
a tenant column is correct until one query forgets the column. Separate
locations make the mistake impossible to write, and they make deletion on
request, retention and export a matter of one location per tenant (PLT-9).

**Why a tenant may change so little.** Wording, the languages requests are
ranked in and what a connector call may cost are choices a tenant owns.
Identity, guardrails, storage, the catalog and where state lives are what make
the platform's guarantees hold for every tenant; a tenant that could change
them could lower them for itself and, through shared infrastructure, for
others (PLT-13).

**Why the host is declared.** A person deserves to know who is answering. A
host inferred from whatever skills happen to be loaded changes when a pack is
added, and a host taken from the caller can be anything the caller types
(PLT-12).

**Why conformance is per domain.** A person talks to a domain, not to a
platform. A platform whose operations domain is adaptive and whose access
domain is governed is exactly that, and averaging the two would describe
neither (PLT-15).

## In the reference implementation

- `oska.platform.packs`: `load_pack`, `check_provides`, `check_requires`,
  `resolve_dependencies` (PLT-1 to PLT-4).
- `oska.platform.versions`: the version ranges.
- `oska.platform.manifest`: `load_platform` (PLT-5).
- `oska.platform.runtime`: `Platform`, one runtime per (tenant, domain) in a
  bounded cache, the session binding and the state layout
  `<state_dir>/<tenant>/<domain>/` (PLT-6 to PLT-9, PLT-16); an evicted
  runtime is closed, a cold build holds a lock of its own pair only, and
  `Platform.close` closes every runtime held (PLT-26).
- `oska.platform.assembly`: the merged bindings, the merged guardrail catalog
  and the refusal of absolute state paths (PLT-9 to PLT-11); the environment
  each binding may read and the fixture confined to its pack, checked when the
  platform is assembled (PLT-22, PLT-23).
- `oska.platform.host`: the catalog wording introduced by the host (PLT-12).
- `oska.platform.tenants`: the tenant overlay, what a tenant may change in a
  binding and the costs it may not raise (PLT-13, PLT-21, PLT-24).
- `oska.platform.conformance`: `run_platform` (PLT-15).
- `oska.platform.runtime`, again: what each domain hands its engine —
  presentation, the merged synonyms and the host — and `Platform._bind`, which
  asks every domain's session store through the storage interface and is
  atomic within one process, holding a session to the tenant and the domain
  of its first turn (PLT-18 to PLT-20, PLT-25).
- `oska forget` and `oska improve` with `--platform`, and optionally `--domain`
  and `--tenant`, reach each (tenant, domain) the platform serves (`OSKA-XC-31`
  in [10-cross-cutting.md](10-cross-cutting.md)).
- `oska.kit`: `oska new pack` and `oska new platform` write a pack or a
  platform repository that validates from the first commit (`oska new pack
  --platform`, or `--into packs` inside a platform directory, names the new
  pack in that platform's manifest), `oska pack check` validates one pack,
  builds the engine a one-pack domain builds, runs its evaluation sets and its
  scenarios and reports its level, refusing a `shell` binding unless
  `--allow-shell` is given, and `oska pack build` writes a content-addressed
  archive that leaves out what is never shipped (PLT-27).
- Channels: `POST /domains/{domain}/sessions/{session_id}/turns`,
  `GET /domains/{domain}/sessions/{session_id}/receipts`,
  `POST /domains/{domain}/proposals/from-session/{session_id}`,
  `GET /domains`, `GET /domains/{domain}/capabilities`; the tenant of a call
  is resolved in `src/oska/api/routes.py`, and a crossing answers the one
  refusal of a session call; the HTTP application closes its platform when it
  shuts down. The command line accepts `--platform` (or `OSKA_PLATFORM`),
  `--domain` and `--tenant`. Without a manifest, the runtime serves one domain
  named `default` (PLT-8, PLT-14, PLT-17, PLT-26, PLT-28, PLT-29).
- Reference platform: `examples/platform.yaml`, with the packs `examples/ops`
  (level 3) and `examples/access` (level 2) as the domains `operations` and
  `access`.
- Tests: `tests/unit/test_platform.py`, `tests/unit/test_overlays.py`,
  `tests/unit/test_kit.py`, and the platform tests in `tests/unit/test_api.py`,
  `tests/unit/test_cli.py` and `tests/unit/test_channel_identity.py`.
