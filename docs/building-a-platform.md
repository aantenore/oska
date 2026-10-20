# Building a platform

The practitioner path from nothing to a governed platform: generate a pack,
understand every file it wrote, replace its mock connector with a real
system, add a second pack, add a second domain, sign your revisions, move
from proxy to token identity, move to PostgreSQL, and wire CI and metrics.
Every command below was run against this repository while this document was
written; where a step needs something this repository does not itself run —
a real system, a real PostgreSQL server — that is said plainly rather than
implied.

[`docs/packs-and-platforms.md`](packs-and-platforms.md) is the contract this
walkthrough builds against; read it first if a term here is unfamiliar.
[`docs/adopting.md`](adopting.md) is the same journey at the level of one
catalog, written before packs existed — this document is the one to follow
when you are starting a platform rather than a single deployment.

## 1. Generate a pack

```sh
oska new pack widgets --entity widget --system inventory --host "Widget Desk"
```

```
wrote /path/to/widgets
```

`--entity` names the record the pack is about (singular; the plural and the
directory names follow from it), `--system` names the integration it binds
to, `--host` fills the pack's own `host:` in `pack.yaml` — documentation of
who this pack is meant to answer as, read by nothing today when the pack is
run on its own; a domain in a platform manifest declares its own `host`
independently, described in step 5. What comes out:

```
widgets/
  pack.yaml
  README.md
  configs/connectors.json
  fixtures/widgets.json
  scenarios/01_greeting.json
  scenarios/02_widget_archive.json
  scenarios/03_off_scope.json
  skills/entity/widget/v1/skill.md
  skills/integration/inventory/v1/skill.md
  skills/runbook/widget_status/v1/skill.md
  skills/runbook/widget_archive/v1/{skill.md,policy.json,evaluation.json}
```

## 2. Read what it wrote before changing anything

The generated `README.md` is the map — it names every file and what to
change first, and it is worth reading in full before you touch the catalog.
The shape is the same one [`docs/adopting.md`](adopting.md) walks by hand:

- `skills/entity/widget/v1/skill.md` — the record: its fields, its
  identifier, a small lifecycle (`active` → `archived`).
- `skills/integration/inventory/v1/skill.md` — the closed list of what can be
  done: a read (`widget.get`) and a governed write (`widget.archive`), the
  write carrying its own `verify` read-back.
- `skills/runbook/widget_status/v1/skill.md` — the read-only procedure. No
  policy: reads are gated by declaration alone.
- `skills/runbook/widget_archive/v1/skill.md` and its `policy.json` — the
  governed write, with the four standard guardrails
  [docs/governance.md](governance.md#guardrails) documents.
- `configs/connectors.json` — binds `integration.inventory` to the mock
  connector over `fixtures/widgets.json`, so the pack runs before it touches
  anything real.
- `scenarios/` — a greeting, an off-scope request, and the archive written as
  a full governed-write scenario: approval, verified receipt, a second
  identical request that the ledger stops.

## 3. Check it

```sh
oska pack check widgets
```

```
widgets 0.1.0: claims level 2, reaches level 2
evaluation: 8 case(s), precision 1.0
scenarios level 1: 2/2 pass
scenarios level 2: 1/1 pass
ok
```

`check` validates the catalog, runs its evaluation sets and its scenarios,
against the pack's own files alone — nothing borrowed. It passes as
generated; run it again after every change.

`oska pack check` reads the runtime configuration, the guardrail catalog and
the JSON schemas the way every other command does, not relative to the pack:
an `OSKA_` variable when one is set, else `configs/` and `schemas/` in the
working directory when they are there, else the defaults the installed
package ships. A pack checks in any directory, including one that has never
seen this repository; `oska config` names the file in force for each under
`sources`. See [docs/configuration.md](configuration.md#packaged-defaults).

## 4. Replace the mock binding with a real system

Nothing under `skills/` changes. `configs/connectors.json` binds
`integration.inventory` to `mock`; point the same integration id at `http` or
`shell` instead, keeping every operation name and payload field the skill
declares:

```json
{
  "version": 1,
  "integrations": {
    "integration.inventory": {
      "type": "http",
      "base_url": "https://inventory.internal.example",
      "headers": { "Authorization": "Bearer ${INVENTORY_TOKEN}" },
      "operations": {
        "widget.get":     { "method": "GET",  "path": "/widgets/{widget}" },
        "widget.archive": { "method": "POST", "path": "/widgets/{widget}/archive",
                             "body_fields": ["widget"] }
      }
    }
  }
}
```

`${INVENTORY_TOKEN}` is read from the environment at connector build time, so
the token never lives in the file. Run `oska pack check widgets` again: the
scenarios still run against the mock unless you rewrite their
`connector_behaviors`, but `oska validate` and `oska run` now reach the real
system. [docs/configuration.md](configuration.md#connectors) has the full
binding reference for `http` and `shell`, including the response-size budget
and the retry classification a real connector answers with — see
[docs/governance.md](governance.md#when-the-system-does-not-answer).

## 5. Add a second pack, and wrap both in a platform

```sh
oska new pack accounts --entity account --system billing --host "Accounts Desk"
oska new platform storefront --pack widgets --pack accounts
```

```
wrote /path/to/accounts
wrote /path/to/storefront
```

```
storefront/
  platform.yaml
  configs/{runtime.json,model.json,guardrails.json}
  packs/widgets/...
  packs/accounts/...
  .env.example  .gitignore  .github/workflows/conformance.yml  README.md
```

`platform.yaml` starts with every pack given inside **one** domain, `default`,
with `default_domain` set to it. The domain's `host` is named after the
platform (`storefront`, not `widgets`'s or `accounts`'s own `pack.yaml` host —
those are not read here), and neither pack's `voice` or `languages` carries
over either; a domain's identity is declared once, in the manifest, and this
is the placeholder to replace, not a value inherited from the packs it lists:

```yaml
domains:
- name: default
  packs: [widgets, accounts]
  host: { name: Storefront, voice: '', languages: [] }
  tenants: [default]
  presentation: false
```

```sh
oska pack check storefront/packs/widgets
oska pack check storefront/packs/accounts
oska conform --platform storefront/platform.yaml
```

```
domain default
pass  01_greeting  level 1  1 turn(s)
pass  02_widget_archive  level 2  3 turn(s)
pass  03_off_scope  level 1  1 turn(s)
pass  01_greeting  level 1  1 turn(s)
pass  02_account_archive  level 2  3 turn(s)
pass  03_off_scope  level 1  1 turn(s)

level 1: 4/4 scenarios pass
level 2: 2/2 scenarios pass
conformance level 2

domain default: conformance level 2
platform storefront: target level 2
```

`conform --platform` checks every domain and reports per domain, then whether
the platform reaches its `target_level` — see
[docs/packs-and-platforms.md#conformance-per-domain](packs-and-platforms.md#conformance-per-domain).
A skill id declared by two packs of the same domain is refused at this point,
so merging packs into one domain is also how you find out two packs actually
collide before a person does; `widgets` and `accounts` share no vocabulary, so
nothing collides here.

Generating a pack straight into an existing platform's `packs/` directory
registers it there without an edit to `platform.yaml` by hand — `--into packs`
finds the `platform.yaml` of the directory it runs in and adds the pack to it,
to its default domain, before the command returns. Run from inside
`storefront/`:

```sh
$ oska new pack support --entity ticket --system helpdesk --into packs
wrote packs/support
named support in /path/to/storefront/platform.yaml, in its default domain
```

`platform.yaml` now names `support` in `packs` and in the default domain's
`packs` list, checked and ready:

```sh
$ oska pack check packs/support
support 0.1.0: claims level 2, reaches level 2
evaluation: 8 case(s), precision 1.0
scenarios level 1: 2/2 pass
scenarios level 2: 1/1 pass
ok
```

## 6. Give each pack its own domain, and a real host

A domain is a scope a person talks to, not a pack — the generator's default of
one domain per platform is a starting point, not a rule. Splitting `widgets`
and `accounts` apart, each behind its own name and voice, is an edit to
`platform.yaml`:

```yaml
domains:
  - name: widgets
    packs: [widgets]
    host: { name: the widgets desk, voice: plain and direct }
    tenants: [default]
  - name: accounts
    packs: [accounts]
    host: { name: the accounts desk, voice: careful and precise }
    tenants: [default]
default_domain: widgets
```

Re-run `oska conform --platform storefront/platform.yaml`: two domains now,
each checked and reported on its own, each answering in its own host's name —
`oska run --platform storefront/platform.yaml --domain accounts --session demo
--text "hello"` now opens with "I am the accounts desk", the way
[docs/packs-and-platforms.md#host-identity](packs-and-platforms.md#host-identity)
describes.

## 7. Sign revisions

Unsigned, a revision is trusted because its directory is never written again
after it is built. Signing adds a second guarantee: a revision the runtime
loads was built with a specific key, and any file added or changed under it
after the fact is refused rather than served quietly.

```json
// configs/runtime.json
"catalog": {
  "signing": { "signer": "hmac-sha256", "key_env": "OSKA_REVISION_KEY", "required": false }
}
```

```sh
export OSKA_REVISION_KEY="a long random value, kept out of the repository"
oska revision build --skills packs/widgets/skills
```

```
e5535dc77255dc84	default	4 skill(s)
  signed by hmac-sha256
```

```sh
oska revision verify e5535dc77255dc84 --skills packs/widgets/skills
```

```
default	e5535dc77255dc84	signed and verified
```

`required: true` refuses to serve an unsigned revision at all — set it once
signing is how every revision in the tenant is built, not while you are still
trying it. A present signature is checked whether or not signing is required,
so a tampered revision is refused either way. The signer is pluggable
(`oska.catalog.signing.register_signer`); `hmac-sha256` is the one shipped.
This is a build-time and start-time check, not a per-request one: a revision
that fails to verify stops the process from serving it, at the moment the
catalog is opened.

## 8. Move from proxy identity to token identity

Proxy identity — the default — trusts `X-Principal-Subject` and
`X-Principal-Attributes` set by whatever sits in front of the port, and
verifies nothing itself. Token identity asks the runtime to verify a signed
token instead, so the runtime is the thing checking who is calling rather
than trusting a header nothing guarantees was set by a proxy and not by the
caller.

```json
// configs/runtime.json
"identity": {
  "mode": "token",
  "key_env": "OSKA_SESSION_TOKEN_KEY",
  "required_claims": ["sub", "sid", "exp"],
  "tenant_claim": "tenant",
  "leeway_seconds": 30
}
```

```sh
export OSKA_SESSION_TOKEN_KEY="another long random value"
```

Minting is not the runtime's job — whoever authenticates the person issues the
token, and the runtime only verifies it. `oska.governance.identity.mint_token`
exists for tests and for an operator's own tooling:

```python
from oska.governance.identity import mint_token
import time
token = mint_token({"sub": "a.operator", "sid": "demo-1", "exp": time.time() + 300},
                    "another long random value")
```

Sent as `Authorization: Bearer <token>`, verified live against a running
application:

```
POST /sessions/demo-1/turns  Authorization: Bearer <token for sid=demo-1>
  200  "Hello. I can check a widget's status and archive a widget."

POST /sessions/other-session/turns  Authorization: Bearer <token for sid=demo-1>
  403  {"error": "session_refused",
        "detail": "this call may not act in this session"}

POST /sessions/demo-2/turns  Authorization: Bearer not-a-real-token
  401  {"error": "invalid_token",
        "detail": "the session token failed the format check: a session token is three base64url parts joined by dots"}
```

The algorithm is fixed at HS256; `alg: none` and anything else is refused
outright, not merely unsupported. `sid` is compared to the session a call
names, and only when the call names one — a session-less call (draft a
proposal, list domains) verifies the token without a `sid` comparison at all,
because there is no session for it to be bound to. Every other claim check
that fails is a `401 invalid_token` naming the check; the `sid` check alone
answers `403 session_refused`, worded the same as every other refusal of a
session, because a token issued for another session is a session problem, not
a malformed token. `tenant_claim` is how a token carries which tenant it is
for, read in
[docs/packs-and-platforms.md](packs-and-platforms.md#tenant).

## 9. Move to PostgreSQL

SQLite is the default and needs nothing installed beyond the base package.
Moving to PostgreSQL is for more than one process sharing one write ledger
and one set of sessions — a deployment with replicas, where two processes
must agree on which write already happened.

```sh
pip install 'oska[postgres]'
```

```json
// configs/runtime.json
"storage": { "backend": "postgres", "url_env": "OSKA_DATABASE_URL" }
```

```sh
export OSKA_DATABASE_URL="postgresql://user:password@host:5432/oska"
```

The ledger's atomicity guarantee is different under each backend, worth
knowing before you rely on it: SQLite serializes claims with a file-level
write lock (`BEGIN IMMEDIATE`), one busy connection waiting on another,
bounded by `storage.busy_timeout_seconds`; PostgreSQL claims a row with
`INSERT ... ON CONFLICT DO NOTHING` and a `SELECT ... FOR UPDATE`, which locks
only the row in question and scales across replicas instead of serializing
every write behind one file. Both satisfy the same contract
(`tests/unit/test_storage_contract.py`), run against SQLite always and against
PostgreSQL when `OSKA_TEST_POSTGRES_URL` names a real server. The `postgres`
job of `.github/workflows/ci.yml` does that on every change: it starts the
official `postgres:16` image as a service container, installs the `postgres`
extra, and runs the contract suite (a claim raced by threads and by separate
processes included) and the conformance suites of both reference catalogs and
of `examples/platform.yaml` on the relational backend, each twice in a row
over the same database, then checks that no conformance namespace is left.
Run the contract suite against your own server too before trusting it under
load, since your version, configuration and load are not the CI's:

```sh
export OSKA_TEST_POSTGRES_URL="postgresql://user:password@host:5432/oska_test"
pytest tests/unit/test_storage_contract.py -q
```

Tenant and domain isolation under a shared PostgreSQL database is by a
`namespace` column (`<tenant>/<domain>`, scoped on every query) rather than by
a separate file per (tenant, domain) the way SQLite keeps it — both satisfy
the same isolation invariant in
[docs/packs-and-platforms.md](packs-and-platforms.md#isolation), by different
means.

## 10. Wire CI and metrics

The generated `.github/workflows/conformance.yml` runs `oska pack check` on
every pack and then `oska conform --platform platform.yaml`, on every push
and pull request. It needs nothing but the runtime and the platform
repository: the platform's own `configs/` is read from the checkout, and the
schemas come with the package. The workflow installs the exact `oska` version
that generated the platform; set the repository variable `OSKA_INSTALL` to a
wheel, an index requirement or a git URL to install from somewhere else.

`GET /metrics` returns the same six numbers `oska slo report` prints — turn
count, latency, fallback rate, verification-failure rate, guardrail-block
rate, unsupported rate — as Prometheus text exposition. It is mounted into the
application unconditionally, and a computed report is served again for
`metrics.cache_seconds` (15 seconds by default) before it is read from the
audit trail and the traces anew, rather than recomputed on every request.

It answers `404 metrics_disabled` when `metrics.enabled` in
`configs/runtime.json` is `false`, and it carries no authentication of its
own — put it behind whatever reverse proxy or network boundary keeps
`/metrics` internal, the same as any Prometheus target. `ops/slo.md` states
the target for each number and `ops/alerts.yml` is the matching Alertmanager
rule set; both name the metrics by the same names the exposition emits, and a
test (`tests/unit/test_metrics.py`) keeps the three in agreement.

The application closes what it holds when it shuts down: a platform, if one
was named, is closed along with every runtime it had assembled, releasing the
connection pools of their bindings rather than leaving them to the process
exit.

## Where this leaves you

A platform with two packs, a signed revision, token identity, and — if you
took step 9 — PostgreSQL underneath it, checked by `oska pack check` per pack
and `oska conform --platform` as a whole, in CI on every push. From here,
[docs/operating.md](operating.md) is the job list of an installation already
running: releasing a new revision, deciding on a proposed change, reading the
service-level numbers, forgetting a conversation on request.
