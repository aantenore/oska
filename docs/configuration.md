# Configuration

Three sources, each with one job. The environment says where things are and
which provider to use. `configs/runtime.json` holds the thresholds and the words
the runtime matches on. `configs/model.json` holds provider access. No threshold
and no matchable word is written in code.

## Environment

To see the values in force, including every override, run `oska config`.
It prints the settings, `runtime.json` and `model.json` as JSON; any scalar
under a key that contains `key`, `token`, `secret` or `password` is shown as
`***`. The guardrail catalog is not among them: it is a document rather than a
set of settings, and `guardrails.catalog` says where to read it.

Every variable is prefixed `OSKA_`. Relative paths resolve against the
current working directory. `.env.example` is a starting point; nothing in the
package loads a `.env` file, so export it yourself:

```sh
set -a; . ./.env; set +a
```

| Variable | Default | Meaning |
| --- | --- | --- |
| `OSKA_SKILLS_ROOTS` | `examples/ops/skills`, `state/skills` | Catalog roots. Separate several with `:` or `,` |
| `OSKA_RUNTIME_CONFIG` | `configs/runtime.json`, else packaged | The runtime configuration file |
| `OSKA_MODEL_CONFIG` | `configs/model.json`, else packaged | The model configuration file |
| `OSKA_GUARDRAILS_CATALOG` | `guardrails.catalog` in `runtime.json`, else packaged | The guardrail catalog file |
| `OSKA_CONNECTORS_CONFIG` | `examples/ops/configs/connectors.json` | The connector bindings |
| `OSKA_SCHEMAS_DIR` | `schemas`, else packaged | Where the JSON schemas live |
| `OSKA_SCENARIOS_DIR` | `examples/ops/scenarios` | Default directory for `oska conform` |
| `OSKA_STATE_DIR` | `state` | Where sessions, receipts, audit and proposals are kept |
| `OSKA_PROVIDER` | unset | `openai_compatible`, `anthropic`, `profiles`, `scripted` or `none`. Overrides `provider` in `model.json` |
| `OSKA_MODEL_BASE_URL` | unset | Overrides `base_url` |
| `OSKA_MODEL_API_KEY` | unset | Overrides `api_key`. The key of the flat shape |
| `OSKA_MODEL_CHAT` | unset | Overrides `chat_model` |
| `OSKA_MODEL_EMBEDDING` | unset | Overrides `embedding_model` |
| `OSKA_PLATFORM` | unset | A platform manifest serving several domains. Same as `--platform` |
| `OSKA_DATABASE_URL` | unset | The address `storage.url_env` names, read only when `storage.backend` is `postgres` |
| `OSKA_REVISION_KEY` | unset | The key `catalog.signing.key_env` names, read only when `catalog.signing.signer` is set |
| `OSKA_SESSION_TOKEN_KEY` | unset | The key `identity.key_env` names, read only when `identity.mode` is `token` |
| `OSKA_TEST_POSTGRES_URL` | unset | Read by the test suite alone, to run the storage contract tests against a real PostgreSQL server. Never read by the runtime |

### Packaged defaults

The runtime configuration, the model configuration, the guardrail catalog, the
phrase catalog and the JSON schemas ship inside the package, under `oska/defaults/`, so a command
works in a directory that holds none of them. Each is resolved in this order:

1. The environment variable (`OSKA_RUNTIME_CONFIG`, `OSKA_MODEL_CONFIG`,
   `OSKA_GUARDRAILS_CATALOG`, `OSKA_SCHEMAS_DIR`), used as given.
2. The default path when it exists relative to the working directory:
   `configs/runtime.json`, `configs/model.json`, `schemas/`, and the catalogs
   `guardrails.catalog` and `reply.phrases` name, looked for there and then
   beside the runtime configuration. The phrase catalog has no environment
   variable.
3. The packaged default.

A guardrail catalog that `runtime.json` names at a path other than the default
is used as named, and a missing one defines no guardrails. `oska config`
prints, under `sources`, the file in force for each of the five, or
`packaged default`. This repository's `configs/` and `schemas/` are
byte-identical to the packaged copies, and a test keeps them so.

An empty or whitespace-only value is treated as unset. The four model overrides
apply on top of `model.json`, so credentials stay out of the repository while
the model names can live in either. They apply to the flat shape only; each
profile of the profiles shape names the variable its own key is read from, in
`api_key_env`, and that variable has no `OSKA_` prefix unless you give it one.

`--skills` on any command replaces the catalog roots for that run.
`--platform`, `--domain` and `--tenant` replace it with a platform manifest and
a part of it instead; see
[docs/packs-and-platforms.md](packs-and-platforms.md#channels).

## `configs/runtime.json`

### `discovery`

How the catalog is ranked against a request.

| Key | Default | Meaning |
| --- | --- | --- |
| `min_score` | `0.2` | Below this, nothing is chosen and the turn is off scope |
| `tie_margin` | `0.05` | When the second candidate is within this of the first, and also clears `min_score`, the result is a tie and the model picks |
| `limit` | `5` | How many candidates are ranked |
| `exclude_types` | `["composite"]` | Skill types that never compete in the ranking |
| `languages` | `["en", "it"]` | The languages a catalog is expected to be reached in |

Only published skills that declare triggers are candidates, so entities and
integrations are never ranked whatever this says. Composites are excluded by
default: they are reached by their triggers on the fast path, or by the model
naming one, rather than competing on a score with the runbooks they contain.

`languages` changes nothing on its own. It is the list `oska enrich run`
derives routing examples for when no `--language` is given, so adding one is
how a deployment says which languages it expects and then asks for a pass. The
cost of adding one is one enrichment pass over the catalog, at publish time.
Removing one leaves the phrases already derived on disk; deleting the derived
files is what removes them.

### `router.rules`

The words the rule layer recognizes without a model. Each is a list; a message
matches only when the whole normalized message is one of them, and only while
the session is waiting for something.

| Key | Applies when | Meaning |
| --- | --- | --- |
| `approve_tokens` | a write is waiting | The message approves it |
| `reject_tokens` | a write is waiting | The message refuses it |
| `cancel_tokens` | a write is waiting, or a goal is under way | The message drops what is under way |

The shipped lists carry English and Italian, and the yes and no of Spanish,
French and German (`sí`/`no`, `oui`/`non`, `ja`/`nein`), which are the words
the packaged phrase catalog asks for in those languages. Add the languages
your operators use; these are also shown to the model router in its prompt, so
the two layers agree on what a yes looks like. The words an approval request tells a person to
type (`approve_word` and `reject_word` in the phrase catalog) must be among these
lists in every language the deployment answers in (`reply.default_language` and
`discovery.languages`), or the runtime refuses to start. An empty list is not
checked.

### `router.fast_path`

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `true` | Whether the layer runs at all |
| `threshold` | `0.75` | The score a trigger match must reach to route without a model |
| `routes.greeting` | a list | Utterances that route straight to a greeting |

The fast path runs only when nothing is pending, matches the message against the
triggers of every published runbook and composite, and hands the turn on if
nothing clears the threshold. Any failure inside it hands the turn on rather than
ending it. The threshold is high on purpose: this layer exists to save a model
call on phrasings an author wrote down, not to guess.

### `ledger`, `audit`, `sessions`, `learning`

| Key | Default | Meaning |
| --- | --- | --- |
| `ledger.path` | `state/ledger.sqlite` | The write ledger |
| `audit.path` | `state/audit.jsonl` | The audit trail, with the `sqlite` backend; with `postgres` it is a table of the database and this path is not read |
| `sessions.path` | `state/sessions.sqlite` | Conversation state, one row per session |
| `learning.publish_root` | `state/skills` | The catalog root a change request is meant to land in |

These are resolved through the state directory: a relative path keeps only its
last segment and is placed under `OSKA_STATE_DIR`, an absolute path is used
as it stands. Every other path in this file that names something mutable —
`tracing.path`, `improvement.learned_root`, `improvement.report_path`,
`catalog.revisions_root`, `changes.root` — is resolved the same way. So setting
`OSKA_STATE_DIR=/var/lib/oska` moves them all at once, and nothing in
`runtime.json` needs to change.

`learning.publish_root` names a root rather than causing anything to be written
there. Nothing publishes into it on its own: `oska change apply` takes the
root as a required argument, and this is the value a person would usually pass.
The conformance runner is the one caller that reads it, standing in for the
person so a level 3 scenario can see the procedure it published.

### `reply`

| Key | Default | Meaning |
| --- | --- | --- |
| `reply.default_language` | `"en"` | The language a session answers in until a turn settles one |
| `reply.conversation_window` | `6` | How many recent messages the reply writer sees |
| `reply.phrases` | `"configs/phrases.json"` | The phrase catalog laid over the packaged one: what the runtime says in its own words, per language. See [multilingual.md](multilingual.md#what-the-runtime-says-in-its-own-words) |

The router reports the language of each message, and once a session has one it
keeps it until another turn changes it.

### `api`

| Key | Default | Meaning |
| --- | --- | --- |
| `api.host` | `127.0.0.1` | Address `oska serve` listens on |
| `api.port` | `8000` | Port it listens on |
| `api.ui` | `true` | Serve the page at `GET /` that holds a conversation through the API; `false` answers 404 there and leaves the API unchanged |

`--host` and `--port` on `oska serve` override these for one run. `oska serve`
warns on stderr before it starts whenever it binds an address other than
loopback while `identity.required` is off: anyone who can reach that port can
then hold a conversation and ask for writes, with nothing but the network in
the way.

### `guardrails`

| Key | Default | Meaning |
| --- | --- | --- |
| `guardrails.enabled` | `true` | Whether the declared guardrails are kept at all |
| `guardrails.catalog` | `configs/guardrails.json` | The document that defines them |

A relative catalog path is looked for in the working directory first and then
beside the runtime configuration file, so a deployment that keeps its
configuration in one directory does not have to say where it is twice.

Setting `enabled` to `false` costs more than it looks. The engine is handed an
empty catalog: the input and output checks stop running, and the instruction
guardrails stop being stated to the model as well, because a deployment that
has switched its promises off should not tell a model it keeps them. Validation
is not affected — it reads the document to learn which identifiers exist
whatever this flag says — so a catalog that still validates is not a catalog
whose promises are still kept. It is a switch for a diagnosis, not a way to
run.

Pointing `catalog` at another document is how a deployment changes what a
guardrail refuses — the patterns, the sets, the wording all live there. The
runtime refuses to load a catalog that names a validator it does not register,
so a typo is a startup failure rather than a promise nothing keeps.

### `tracing`

| Key | Default | Meaning |
| --- | --- | --- |
| `tracing.enabled` | `false` | Whether spans are recorded |
| `tracing.path` | `state/traces.jsonl` | Where they are appended, resolved under the state directory |
| `tracing.exporter` | `file` | `file`, or `otlp` to also send every span to an OpenTelemetry collector; the file is written either way |
| `tracing.otlp.endpoint` | `null` | The collector's traces endpoint: a full URL such as `http://collector:4318/v1/traces` for `http/protobuf`, or `host:port` or a URL for `grpc`. Unset, the standard `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` or `OTEL_EXPORTER_OTLP_ENDPOINT` decides, and without either the SDK's local default |
| `tracing.otlp.protocol` | `http/protobuf` | `http/protobuf` or `grpc` |
| `tracing.otlp.service_name` | `oska` | The `service.name` resource attribute spans are exported under |
| `tracing.otlp.include_session` | `false` | Add the session id to every exported span. Off, because a collector keeps what it receives and forgetting a session cannot reach it; turn on only with opaque session ids and a collector retention no longer than `retention` |
| `tracing.otlp.headers_env` | `null` | Names the environment variable holding the export headers, as `key=value,key=value` with values URL-encoded. Unset, `OTEL_EXPORTER_OTLP_HEADERS` applies. A named variable that is unset stops the engine from starting |

Off by default and safe to leave off. Traces are diagnostics: how long each
stretch of a turn took and what it was nested in. They are not the audit
record, nothing reads them back, and no decision is taken from one. Turning
them on costs one appended line per span and a file that grows without bound —
there is no rotation — so it is something to switch on while a question is open
and off again afterwards. A turn that is being traced emits a `trace` audit
event naming where the spans are going, so the audit trail records that a
diagnostic file exists.

`exporter: otlp` needs the optional extra `otel` (`pip install 'oska[otel]'`);
without it the engine refuses to start and names the extra. The endpoint may
not carry credentials and `tracing.otlp` refuses a `headers` key: whatever the
collector needs to authenticate goes in the variable `headers_env` names. The
standard `OTEL_*` variables the SDK reads, such as `OTEL_TRACES_SAMPLER` and
`OTEL_RESOURCE_ATTRIBUTES`, apply as well. What an exported span carries is in
[Sending traces to your collector](operating.md#sending-traces-to-your-collector).

### `turn`

| Key | Default | Meaning |
| --- | --- | --- |
| `turn.max_model_calls` | `4` | Completions one turn may make. Embeddings are not counted |

An ordinary new task spends three — classify, plan, write the reply — which
leaves one for a tie break or a plan repair. A turn that asks for one more than
this does not get it: the model is not called, the pending goal is dropped, and
the turn answers saying it reached the calls it is allowed to make.

Lowering it makes turns cheaper and makes long ones fail; two is enough for a
greeting and not enough for a governed write. Raising it buys longer turns and
weakens the bound that keeps a feature from being paid for on every turn, which
is the reason the key exists.

### `enrichment`

Routing examples derived from the declared triggers, written at publish time
and read by the index. See [multilingual.md](multilingual.md).

| Key | Default | Meaning |
| --- | --- | --- |
| `enrichment.enabled` | `false` | Whether the runtime reads derived phrases, and whether publication produces them |
| `enrichment.per_language` | `3` | How many phrases are asked for per language, per procedure |
| `enrichment.cap_per_route` | `24` | How many texts one skill may contribute to the index |
| `enrichment.filename` | `generated-examples.json` | Name of the derived file, written inside the skill's version directory |

`enabled` is what the runtime consults, not what `oska enrich run` consults:
a person running the command is asking for the files whatever the flag says.
With the flag off, the files are written and ignored; turning it on is what
makes them count in routing, and costs nothing per turn, because the phrases
are read from disk.

`per_language` is the size of the pass, and the only thing that costs model
calls — one call per procedure that is missing a language. `cap_per_route`
bounds the index, never below what the author declared: a declared trigger is
never dropped to make room for a derived phrase. Changing `filename` orphans
the files already written under the old name.

### `improvement`

Bounds the scheduled improvement job works inside. See
[improvement.md](improvement.md).

| Key | Default | Meaning |
| --- | --- | --- |
| `improvement.learned_root` | `state/learned` | Where learned phrasings are kept, one directory per procedure. The index reads them; nothing writes them during a turn |
| `improvement.report_path` | `state/improvement.json` | Where the report of the last run is written |
| `improvement.max_phrases_per_skill` | `24` | How many learned phrasings one procedure may hold |
| `improvement.cluster_threshold` | `0.8` | How alike two unanswered requests must be to count as the same one |
| `improvement.cluster_min_size` | `3` | How many requests a cluster needs before it is reported as a gap |
| `improvement.bounds` | `min_score` 0.1 to 0.5, `tie_margin` 0.02 to 0.15 | The tunables the job may move, and the interval it may move them in |

Both paths are resolved under the state directory by the rule described above.
Everything the job writes is under them, and deleting either undoes the job
without touching the catalog.

`bounds` is the ceiling on tuning, and a tunable absent from it is not tunable
— the job reports that it left the value alone. Widening an interval lets the
search travel further from the number a person chose; setting it wide enough
and the search will discover that a floor of zero answers every request
decisively, which is what a system that has stopped declining looks like on a
report. `cluster_min_size` is the line between noise and a missing capability:
lowering it produces more drafts for a reviewer to read, raising it hides
patterns until they are common.

### `catalog`

Which catalog this deployment serves, and how long a session keeps the one it
started on. See [operating.md](operating.md).

| Key | Default | Meaning |
| --- | --- | --- |
| `catalog.revisions_root` | `state/revisions` | Where built revisions are kept, one directory per tenant, resolved under the state directory |
| `catalog.revision` | `""` | The revision to serve. Empty means the one the store records, or failing that the newest it holds |
| `catalog.tenant` | `"default"` | The catalog a deployment serves when it serves more than one |
| `catalog.pin_sessions` | `true` | Whether a session keeps the revision it started on |
| `catalog.mode` | `"directory"` | `"directory"` serves a built revision when the tenant has one and the skill roots otherwise; `"revision"` refuses to serve anything but a built revision |
| `catalog.signing.signer` | `""` | The name of a registered signer (`hmac-sha256` ships with the runtime) a revision is signed and verified with. Empty means unsigned |
| `catalog.signing.key_env` | `"OSKA_REVISION_KEY"` | The environment variable the signer reads its key from |
| `catalog.signing.required` | `false` | Whether an unsigned revision is refused. A present signature is checked whatever this says |

With no revision built for the tenant, the configured skill roots are read
directly and reload when the files under them change. That is what one process
on its own disk wants and what the example catalogs use; several replicas want
a revision, because a directory tells each of them about a change at a
different moment. `catalog.mode: "revision"` is the stricter version of the
same choice: a deployment that must never silently fall back to reading a
directory sets it, and a tenant with nothing built then refuses to serve at
all rather than serving something nobody released.

**Signing.** `oska revision build` signs a revision when `catalog.signing.signer`
names a registered signer and the environment holds its key; building again
over an already-held revision re-signs it in place. `oska revision verify` and
the check the runtime itself runs before serving a revision both fail closed:
a signature that does not match the key, or a file whose hash disagrees with
the signed manifest, is refused whether or not signing is `required` — only
the case of no signature at all is let through when it is not required.
[docs/building-a-platform.md#7-sign-revisions](building-a-platform.md#7-sign-revisions)
walks it end to end.

Naming a `revision` pins the deployment to it and takes the choice away from
`oska revision use`, which is right for a rollback under review and wrong as
a standing setting, because the file then has to be edited and redeployed to
move a release. A named revision the store does not hold falls back to the
configured skill roots rather than to another revision, and the catalog check
the factory runs is what then decides whether anything is served at all.

Setting `pin_sessions` to `false` lets every turn follow the deployment, at the
cost that a conversation can be told one thing and then answer from another
catalog. An approval is held to its revision either way, because the sentence a
person approved was composed from it.

### `changes`

| Key | Default | Meaning |
| --- | --- | --- |
| `changes.root` | `state/changes` | Where a proposed catalog change is written for a person to review, resolved under the state directory, with the `sqlite` backend; with `postgres` the requests and their packages are tables of the database and this path is not read |

One directory per request, holding the package laid out the way a catalog root
is laid out, the request record and a summary. Moving it moves where a reviewer
looks; it changes nothing about who may apply one, because `oska change
apply` takes the catalog root as a required argument and there is no configured
value it could fall back to.

### `resilience`

What a connector is allowed to cost before a capability is called unavailable.
See [governance.md](governance.md#when-the-system-does-not-answer).

| Key | Default | Meaning |
| --- | --- | --- |
| `resilience.timeout_seconds` | `30.0` | Seconds one call may take before it counts as never having arrived |
| `resilience.failures_before_open` | `3` | Consecutive calls whose failure was `transport` (retryable or ambiguous) before the binding stops being called |
| `resilience.recovery_seconds` | `30.0` | How long it stays out of service before one attempt is let through |
| `resilience.retries` | `0` | Further attempts a **read** gets inside the same call after a retryable result, or one whose status is in `retry_statuses`. A write is never retried inside a call |
| `resilience.backoff_seconds` | `0.5` | The pause before the first retry; it doubles on each further one |
| `resilience.retry_statuses` | `[502, 503, 504]` | HTTP statuses that make a read worth retrying even when the connector classified the failure `ambiguous` rather than `retryable` |
| `resilience.max_response_bytes` | `1000000` | A response larger than this is never read to the end. Classified `refused`, terminal — the response existed, it was simply too large to trust |

Every one of these costs has a floor: `timeout_seconds` and
`max_response_bytes` above zero, `failures_before_open` at least one,
`recovery_seconds`, `retries` and `backoff_seconds` never negative. The floor
is enforced wherever this configuration is read, a tenant's fragment
included — a tenant may lower `retries`, `timeout_seconds`,
`backoff_seconds` or `max_response_bytes` below the platform's own value (see
[docs/packs-and-platforms.md#tenant-overlay](packs-and-platforms.md#tenant-overlay)) but
never past the floor; a value below it is refused the same way one above the
platform's ceiling is.

The same holds outside connectors. `enrichment.per_language`,
`enrichment.cap_per_route`, `improvement.max_phrases_per_skill` and
`improvement.cluster_min_size` are at least one, `improvement.cluster_threshold`
lies above zero and at most one, `storage.busy_timeout_seconds` is above zero,
and `retention.days` is never negative. A configuration that goes below any of
them is refused when it is read, not when the value is first used.

These apply to every binding the connector registry builds, and an integration
may declare its own `resilience` block to override any of them for that
binding alone. Only failures classified `transport` count toward the breaker:
a system that answered no, or whose answer was too large, is a system that is
up. See [docs/governance.md#when-the-system-does-not-answer](governance.md#when-the-system-does-not-answer)
for what `retryable` and `ambiguous` mean for the write path and the ledger,
which is a different question from what this section governs — in-call
retries and the breaker are about whether and how often a call is attempted at
all.

Raising the timeout makes a slow system look healthy and a turn take longer —
it is spent inside the turn, so it competes with everything else the turn has
to do. Lowering `failures_before_open` to one takes a binding out of service on
a single blip; raising it means more turns wait for a system that is already
known to be down. `recovery_seconds` is how quickly a recovered system is
noticed, and the cost of shortening it is one call against a system that may
still be broken. Raising `retries` spends more of a turn's time budget on one
read before giving up on it; `retry_statuses` is scoped to reads because a
write already has its own, safer path back from an inconclusive answer — the
ambiguous-write settlement described in `docs/governance.md`, not a blind
resend. `max_response_bytes` is a budget on trust as much as on memory: a
response too large to read within it is refused rather than read partially
and treated as complete.

### `retention`

| Key | Default | Meaning |
| --- | --- | --- |
| `retention.days` | `0` | How long the record of a conversation is kept. Zero keeps everything |

The window is what `oska forget expired` applies when it is given no
`--days`, measured from the moment each session was last written. Nothing
applies it on its own: no turn expires anything, and the serving process
removes nothing, so a deployment that wants a window has to run the command on
a schedule. Deletion on request, `oska forget session`, is answered whatever
this says.

Setting it costs what it sounds like it costs. The receipts, the audit entries
and the stored state of a conversation past the window are gone together, and
the account of what was written goes with them — which is the point, and is
also why the number deserves a decision rather than a default.

### `storage`

| Key | Default | Meaning |
| --- | --- | --- |
| `storage.backend` | `"sqlite"` | `"sqlite"` or `"postgres"` |
| `storage.url_env` | `"OSKA_DATABASE_URL"` | The environment variable the address is read from, only when the backend is `postgres` |
| `storage.busy_timeout_seconds` | `10.0` | SQLite: how long a connection waits for another to finish before it gives up. PostgreSQL: the connect and lock timeout derived from the same value |

The backend keeps five stores: the ledger, the sessions, the audit trail, the
proposals and the change requests with their packages. With `sqlite` they are
files under the state directory: the two SQLite files, `audit.path`,
`proposals/` and `changes.root`. With `postgres` all five are tables of one
database, so several replicas share one ledger, one set of sessions, one audit
trail (and so one service-level report and one `/metrics`), and one review
queue.

With `sqlite`, the sessions and the ledger are files, and two turns of the same
process may write at once; this is how long one waits for the other rather
than failing the turn. Raising it is worth doing only when turns start failing
on a busy file rather than on the system they were talking to; lowering it
turns contention into a failed turn sooner, which is a diagnosis and not a
setting to run with. With `postgres`, both stores go through `psycopg`
(the `postgres` extra: add `[postgres]` to the requirement you installed `oska`
with, as in `pip install 'oska[postgres]==<version>'`) against the address
`url_env` names, with numbered migrations applied under an advisory lock the
first time a process opens the database. The two backends satisfy the same
`LedgerStore`, `SessionStateStore`, `AuditStore`, `ProposalRecordStore` and
`ChangeRequestRecordStore` protocols in `oska.storage`, run through one
contract test suite (`tests/unit/test_storage_contract.py` and
`tests/unit/test_storage_contract_shared.py`) — always against the files
backend, and against PostgreSQL when `OSKA_TEST_POSTGRES_URL`
names a real server, which the `postgres` job of this repository's CI does
against a PostgreSQL 16 service container.
Choosing `postgres` is for more than one process sharing one record, where SQLite's single-file lock would serialize every write
across every replica instead of only the ones that collide.

Isolating a (tenant, domain) pair's state differs by backend as well. SQLite
keeps each under its own file path, `<state_dir>/<tenant>/<domain>/`; a shared
PostgreSQL database keeps them apart with a `namespace` column
(`<tenant>/<domain>`) scoped on every query instead. Both satisfy the same
isolation invariant in
[docs/packs-and-platforms.md#isolation](packs-and-platforms.md#isolation), by
different means, and [docs/building-a-platform.md#9-move-to-postgresql](building-a-platform.md#9-move-to-postgresql)
walks moving from one to the other.

### `identity`

| Key | Default | Meaning |
| --- | --- | --- |
| `identity.mode` | `"proxy"` | `"proxy"` trusts headers a fronting proxy sets and verifies nothing; `"token"` verifies a signed session token itself |
| `identity.required` | `false` in `proxy` mode, `true` in `token` mode | Whether a call with no identity is refused, `401 identity_required`. `GET /health` answers without one whatever this says; `GET /metrics` does too only where `metrics.public` says so |
| `identity.key_env` | `"OSKA_SESSION_TOKEN_KEY"` | The environment variable a token's HS256 key is read from, only in `token` mode |
| `identity.required_claims` | `["sub", "sid", "exp"]` | Claims a token must carry |
| `identity.tenant_claim` | `"tenant"` | Which claim a platform reads to resolve the tenant of a call |
| `identity.tenant_header` | `""` | In `proxy` mode only, the header a fronting proxy sets to say which tenant a call is for; empty means no header is read at all, and once named it is the only place the tenant is read from |
| `identity.proxy_secret_env` | `""` | In `proxy` mode only, the variable holding a secret the proxy sends with every call. Named, a call carrying identity or tenant headers without it is refused with `401 proxy_unverified`; empty, the headers are trusted as they arrive, and `oska serve` warns when that happens on a non-loopback address |
| `identity.proxy_secret_header` | `"X-Proxy-Secret"` | The header the proxy puts that secret in |
| `identity.reviewer_role` | `"reviewer"` | The role, in `roles` on the principal, a call must carry to read or move a proposal or a change request |
| `identity.leeway_seconds` | `30` | Clock skew allowed on `exp` and `nbf` |
| `identity.single_user` | `false` | Whoever reaches this runtime without an identity does a reviewer's work as its one user. Off by default; the `runtime.json` this package ships, and `oska new platform` copies, sets it `true` for a local single-user setup. Refused in `token` mode |

Turning `required` on in `proxy` mode is what makes a proxy that forgot to set
its headers fail loudly instead of quietly recording writes under no
principal at all; turning it off in `token` mode is what lets a deployment
that verifies tokens still answer a call that presents none — worth doing
only for a route meant to have no identity, since every other route pays for
it the same way. `tenant_header` costs nothing until it is set: naming it is
what lets a proxy say which tenant a call is for, and a header the
configuration does not name is never read as one, whatever a client sends —
and once it is named, a `tenant` entry in the attributes header is never read
either. `reviewer_role` is the one string every reviewing principal's `roles`
must carry; changing it does not revoke anything already granted, it only
changes which name the check looks for next. `single_user` is what lets the
packaged and the generated defaults answer a first request with no proxy and
no token in front of them at all: whoever reaches the port is that one user,
reviewer included, until the deployment is moved past it — see
[src/oska/templates/platform/README.md](../src/oska/templates/platform/README.md)
("Who can talk to it") for the steps.

See
[docs/governance.md#who-may-do-what-over-http](governance.md#who-may-do-what-over-http)
for what each mode does with a turn and who may reach which route, and
[docs/building-a-platform.md#8-move-from-proxy-identity-to-token-identity](building-a-platform.md#8-move-from-proxy-identity-to-token-identity)
for moving a deployment from one mode to the other.

### `evaluation`

| Key | Default | Meaning |
| --- | --- | --- |
| `evaluation.min_precision` | `1.0` | The floor `oska evaluate catalog` holds the catalog's aggregate precision to |
| `evaluation.min_recall` | `1.0` | The floor it holds aggregate recall to |
| `evaluation.require_sets` | `false` | Whether a skill with no evaluation set fails the gate on that alone |

`evaluate_catalog` aggregates every case of every set in the catalog into one
precision and one recall, and a run whose either number falls below its floor
exits non-zero — a hard gate, not merely a report, and `.github/workflows/ci.yml`
runs it on every push. Both default to `1.0`, so a catalog that ships any
evaluation sets at all currently has to answer every one of their cases
correctly; lowering either floor is how a deployment tolerates some
ambiguity in its own routing on purpose, deliberately, rather than by not
measuring it. `require_sets` is a separate question from precision and
recall — a catalog with perfect precision and recall on the sets it has can
still fail this if a skill carries none.

### `metrics`

| Key | Default | Meaning |
| --- | --- | --- |
| `metrics.enabled` | `true` | Whether `GET /metrics` answers at all |
| `metrics.cache_seconds` | `15.0` | How long a computed report is served again before it is read from the audit trail anew |
| `metrics.public` | `false` | Opens `GET /metrics` to a caller with no identity even where `identity.required` is on |

`false` makes the route answer `404 metrics_disabled` rather than an empty or
zeroed body, so a scraper configured against a deployment that has turned
metrics off gets a clear signal instead of a misleadingly healthy-looking
response. `GET /metrics` is mounted into the application unconditionally —
`enabled` decides only what it answers, not whether the route exists — see
[docs/building-a-platform.md#10-wire-ci-and-metrics](building-a-platform.md#10-wire-ci-and-metrics).

A report names every tenant a platform serves and what each does, so where
`identity.required` is on the route is a reviewer's read like the proposals
are: a principal carrying `identity.reviewer_role`, and nobody without an
identity. `public` opens it anyway, for a scraper that reaches the runtime
over a private network and presents no identity; turning it on is a statement
about that network, not about the runtime, so it stays off by default. Where
no identity is required the route stays open, as every other read there is.

Lowering `cache_seconds` trades audit-trail reads for fresher numbers; `0`
recomputes the report on every scrape, which is correct and costs one full
read of the trail each time. Raising it is worth doing once a deployment's
scrape interval is known and stable — a value longer than that interval just
serves the same numbers to every scrape in between it.

### `platform`

| Key | Default | Meaning |
| --- | --- | --- |
| `platform.cache_size` | `16` | How many (tenant, domain) runtimes a platform keeps assembled at once |

Only meaningful with `--platform` or `OSKA_PLATFORM`. A runtime evicted from
this bound is rebuilt from the same declarations the next time it is asked
for, and resumes from the state it left — eviction costs the work of
reassembly, never any state. See
[docs/packs-and-platforms.md](packs-and-platforms.md#where-this-sits-in-the-codebase).

## `configs/model.json`

The file describes the model provider in one of two shapes. The flat shape
names one endpoint. The profiles shape names several, and says which of them
answer completions and which answer embeddings, in the order they are tried.

| Key | Default | Meaning |
| --- | --- | --- |
| `provider` | `"none"` | `openai_compatible`, `anthropic`, `profiles`, `scripted` or `none` |
| `base_url` | `null` | Flat shape: endpoint root |
| `api_key` | `null` | Flat shape, deprecated: the key, sent with every call. Use `OSKA_MODEL_API_KEY` instead |
| `chat_model` | `""` | Flat shape: model name for completions |
| `embedding_model` | `""` | Flat shape: model name for embeddings |
| `timeout_seconds` | `30.0` | Flat shape: HTTP timeout |
| `profiles` | `{}` | Profiles shape: the endpoints, by name |
| `roles` | unset | Profiles shape: `chat` and `embeddings`, each a list of profile names |

The file in the repository names no endpoint and no model. It carries
`chat-model-name` and `embedding-model-name`, and `.env.example` carries
`https://your-provider.example/v1`, because the runtime has no preference among
endpoints that speak the protocol. Replace the three with the endpoint and the
model names of the provider you run against.

With `provider: "none"` the runtime routes by rule and by trigger, ranks the
catalog by keyword overlap, answers what it can do, and declines to plan. That
is a working configuration: `validate`, `capabilities`, `ontology` and the
greeting and off-scope paths all work without a provider.

`openai_compatible` speaks the chat-completions protocol. It asks for the answer
in three ways, each only when the one before it was refused with an HTTP 400 or
422 whose body names the response format, the schema or structured output: a
response constrained to the JSON schema, then a JSON object with the schema
written into the system message, then no response format with the schema still
in the system message. The first JSON object in the answer is taken. Any other
refusal is reported as it is.

`anthropic` speaks the messages protocol. It posts to `{base_url}/v1/messages`
with the key in `x-api-key`, declares the schema as the input of one tool the
model is required to call, and takes the input it is called with as the answer.
System messages in the conversation are folded into the system prompt, and
consecutive messages of one role are merged. The protocol serves no embeddings,
so with this provider the catalog is ranked by keyword.

`scripted` replays recorded answers. On the command line it needs `--script`
pointing at a file: a JSON list of steps, or an object with a `provider_script`
or `script` key — which is why a scenario file can be handed to it directly.

Keep the key out of the file. `OSKA_MODEL_API_KEY` exists for that; the flat
`api_key` key is still read, and is deprecated.

`oska run`, `oska enrich` and `oska serve` build the provider the same way.
`serve` reasons with it in both modes, over one catalog and over every domain
of a platform. A provider that cannot be built stops the command with one line
before anything runs or listens.

### Profiles and roles

`provider: "profiles"` reads `profiles` and `roles` and ignores the flat keys and
the four `OSKA_MODEL_*` overrides. Each profile takes:

| Key | Default | Meaning |
| --- | --- | --- |
| `adapter` | required | `openai_compatible` or `anthropic`: the protocol the endpoint speaks |
| `base_url` | required | Endpoint root |
| `api_key_env` | `""` | The environment variable the key is read from. Empty: the endpoint is called without a key |
| `chat_model` | `""` | Model name for completions. Required for a profile in `roles.chat` |
| `embedding_model` | `""` | Model name for embeddings. Required for a profile in `roles.embeddings` |
| `timeout_seconds` | `30.0` | HTTP timeout |
| `max_tokens` | unset | The bound on an answer. `openai_compatible` sends it only when it is set; `anthropic` always sends one, `4096` when it is not set |
| `headers` | `{}` | Headers sent as written with every call of the profile |

```json
{
  "provider": "profiles",
  "profiles": {
    "primary": {
      "adapter": "openai_compatible",
      "base_url": "https://primary.example/v1",
      "api_key_env": "PRIMARY_MODEL_KEY",
      "chat_model": "chat-model-name",
      "embedding_model": "embedding-model-name"
    },
    "secondary": {
      "adapter": "anthropic",
      "base_url": "https://secondary.example",
      "api_key_env": "SECONDARY_MODEL_KEY",
      "chat_model": "other-chat-model-name",
      "max_tokens": 4096
    }
  },
  "roles": {
    "chat": ["primary", "secondary"],
    "embeddings": ["primary"]
  }
}
```

A key is read only from the variable `api_key_env` names; a profile that holds
an `api_key` of its own is refused when the file is read. A profile whose
variable is unset or empty is skipped, with a warning on standard error that
names the variable and never a value. When no chat profile is left, the
provider is not built and the error names the variables that were missing.

`roles` is checked when the file is read: a name that is not a declared profile
is refused, and so is an empty `chat` role, a chat profile without a
`chat_model` and an embeddings profile without an `embedding_model`.

A completion goes to the first chat profile. It moves to the next only when the
failure is retryable, and otherwise raises at once:

| Retryable: tried on the next profile | Not retryable: raised |
| --- | --- |
| No connection, a timeout, any other transport failure | HTTP 400, 401, 403, 404, 422 |
| HTTP 408, 429 and every 5xx | Any other status below 500 |
| An answer that cannot be read: no JSON object, an empty message, no tool call | |

A request an endpoint refused as it was written would be refused by the next
one too; sending it on would only hide a misconfiguration and spend the time of
every endpoint after it. When the last profile fails, its error is raised.

Embeddings go to the `embeddings` profiles the same way. The role may be empty:
the catalog is then ranked by keyword, exactly as with no provider, while the
chat profiles still route, plan and phrase.

A completion that took two profiles to answer is still one call against
`turn.max_model_calls`. A turn in which any call moved from one profile to the
next emits one `model_fallback` audit event: which profile answered each call,
and each fallback with its role, the profile left, the profile tried next and
the reason, an HTTP status or the class of the failure. It never carries what
was asked or answered. Whichever profile answered, a plan or a reply is
validated exactly as any other.

## Connectors

The connector configuration binds each integration id to the thing that runs its
operations. Relative fixture paths resolve against the configuration file
itself.

```json
{
  "version": 1,
  "integrations": {
    "integration.container_runtime": {
      "type": "mock",
      "fixture": "../fixtures/services.json",
      "operations": { "service.status": { "collection": "services", "match": "name",
                                          "payload_field": "service", "as": "service" } }
    }
  }
}
```

### `mock`

Reads and writes an in-memory copy of a JSON fixture, so a session changes state
and the file on disk does not. Needs `fixture`. The operation mapping keys —
`collection`, `match`, `payload_field`, `where`, `filter`, `field_map`,
`project`, `as`, `limit_field`, `limit_default`, `contains`, `set`, `append` —
are documented in [`examples/ops/README.md`](../examples/ops/README.md).

### `http`

| Key | Meaning |
| --- | --- |
| `base_url` | Root the paths are relative to |
| `headers` | Sent on every request |
| `timeout` | Seconds. Defaults to 30 |
| `operations.<name>.method` | `GET`, `POST`, and so on |
| `operations.<name>.path` | Path template; `{field}` placeholders are filled from the payload |
| `operations.<name>.query_fields` | Payload fields that become the query string |
| `operations.<name>.body_fields` | Payload fields that become the JSON body |

Nothing else of the payload leaves the process. A field that is not named in the
path, the query or the body is not sent.

Every call whose method is not `GET`, `HEAD`, `OPTIONS` or `TRACE` carries an
`Idempotency-Key` header, so the far end has the same "same write" signal the
ledger does. For a governed write it is derived from the ledger key itself
(`idempotency_key(session_id, ledger_key)`), so a retried approval and the
system's own deduplication agree on what counts as a repeat; for any other
unsafe call it is derived from the request's own shape — the operation, the
path, the query and the body — so two identical calls always carry the same
key and two different ones never do.

### `shell`

| Key | Meaning |
| --- | --- |
| `timeout` | Seconds. Defaults to 30 |
| `operations.<name>.argv` | The argument vector, with `{field}` placeholders |
| `operations.<name>.cwd` | Working directory |
| `operations.<name>.timeout` | Overrides the binding's timeout |

The vector is declared up front and rendered one argument at a time, after each
value is checked. The command is never assembled into a string and never handed
to a shell, so a payload value can only ever become one argument. It can never
become an option either: a value that would start an argument with `-` (say
`--kubeconfig=/elsewhere` in `["kubectl", "rollout", "restart", "{service}"]`) is
refused before the command runs. Where a dashed value is legitimate, such as a
negative number, declare `--` before it in the vector; a dash the vector itself
declares (`"--name={value}"`) is always kept.

### `mcp`

Binds each operation to one tool on a server speaking the Model Context
Protocol. This is a client: the server's list of tools is never shown to the
model and never consulted during a turn, so only the operations a skill
declares and this binding maps can be called.

```json
{
  "integration.tickets": {
    "type": "mcp",
    "transport": "stdio",
    "argv": ["tickets-server", "--read-write"],
    "env": { "TICKETS_TOKEN": "${TICKETS_TOKEN}" },
    "operations": {
      "ticket.get":   { "tool": "get_ticket",   "arguments": { "id": "{ticket_id}" } },
      "ticket.close": { "tool": "close_ticket", "arguments": { "id": "{ticket_id}",
                                                             "reason": "{reason}" } }
    }
  }
}
```

| Key | Meaning |
| --- | --- |
| `transport` | `stdio` or `http`. Required |
| `argv` | `stdio`: the argument vector the server is started from, for each call. Never run through a shell |
| `env` | `stdio`: variables the server is started with. It inherits `PATH` (and `SYSTEMROOT` on Windows) and nothing else of this process's environment |
| `cwd` | `stdio`: working directory, relative to this file |
| `url` | `http`: the endpoint every message is posted to (streamable HTTP; a JSON or an event-stream answer) |
| `headers` | `http`: sent with every message |
| `timeout` | Seconds. Defaults to 30. For `stdio` it bounds the whole exchange, start-up included; for `http` each request |
| `operations.<name>.tool` | The one tool this operation calls |
| `operations.<name>.arguments` | The arguments template. Absent means no arguments |
| `operations.<name>.timeout` | Overrides the binding's timeout |

The arguments are built from the template one value at a time. A string that
is exactly `"{field}"` becomes the payload value of that field with its type
(a number stays a number, a list stays a list), and is left out when the
payload does not carry the field. A placeholder inside a longer string
(`"ticket {ticket_id}"`) needs a single value and is refused, before anything
starts, when the payload has none. Keys are never filled, so a payload cannot
add or rename an argument, and a payload field the template does not name is
never sent. Literal numbers, booleans and nested objects in the template are
sent as written.

A call is `initialize`, `notifications/initialized`, then `tools/call`. A
result with `isError` is the system saying no (`terminal`), and its text is the
reason. Otherwise `structuredContent` is the data when present, else the first
text content that parses as JSON, else `{"text": ...}` with the text. Anything
that goes wrong before `tools/call` is sent, including a server that cannot
start or does not answer `initialize` in time, is `retryable`; a tool call that
times out, or whose server exits or ends its stream before answering, is
`ambiguous`. The protocol has no idempotency key, so none is sent; an
`ambiguous` write is settled by its read-back.

`oska pack check` refuses a `stdio` binding without `--allow-shell`, because
it starts a command on this machine. A check that may reach the server asks it
for its tools and reports an operation bound to a tool the server does not list
as an error; a server it cannot reach is reported as not checked, in a warning.
`oska validate` reads only the catalog and never contacts a server.

### `${ENV}` expansion

`${NAME}` inside an address or a header is replaced by the environment variable
of that name, so `"Authorization": "Bearer ${RUNTIME_TOKEN}"` keeps the token out
of the file. A variable that is not set becomes an empty string and is recorded
in `ConnectorRegistry.warnings`; the binding is still built, and the call fails
at the far end rather than at load time. Expansion applies to `base_url` and
header values, and for `mcp` to `argv`, `env` values, `url` and header values.
Nothing of the payload is ever put in a header or an environment variable.
Served from a platform, every variable a binding reads must be one its domain's
`environment` grants.

`examples/ops/configs/connectors.real.example.json` is the same catalog bound to
an HTTP API and a command line tool. The operation names and payload fields are
the ones the integration skills declare and do not change with the binding,
which is the point: swapping the file swaps the systems, not the catalog.

## The state directory

Everything mutable lives under `OSKA_STATE_DIR`, `state/` by default, and
`state/` is in `.gitignore`.

```
state/
  sessions.sqlite     session state: history, principal, revision, pending goal, pending approval, language
  ledger.sqlite       write receipts
  audit.jsonl         one JSON event per line (sqlite backend; a table with postgres)
  traces.jsonl        one span per line, only when tracing is on
  proposals/          one JSON file per proposal, named by proposal id (sqlite backend)
  changes/            one directory per change request: the package, the request, the summary (sqlite backend)
  revisions/          one directory per tenant, holding its built revisions and the CURRENT file
  skills/             a catalog root an applied change request lands in
  learned/            one directory per procedure, holding the phrasings learned for it
  feedback/           one file per session, holding the judgments recorded on its answers
  preferences/        one file per session, holding what was learned about how it reads
  improvement.json    the report of the last improvement run
  discovery-overrides.json   tuned thresholds, written only when a run is told to apply them
```

`sessions.sqlite` always sits directly under the state directory; the others
are placed there by the resolution rule described above. `state/skills` is one
of the default catalog roots, which is where `oska change apply` is usually
pointed. Nothing under `learned/`, `feedback/` or `preferences/` is authored:
deleting a file undoes what it held and nothing else.

A revision directory is written once and never again, so the contents of
`revisions/` are the only thing here that is safe to copy between machines as
it stands. Deleting one a session is pinned to is a way to break that session.

Delete the directory to start clean. Delete `ledger.sqlite` alone and the
runtime forgets which writes it has already done, which is exactly as dangerous
as it sounds.
