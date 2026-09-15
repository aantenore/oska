# Configuration

Three sources, each with one job. The environment says where things are and
which provider to use. `configs/runtime.json` holds the thresholds and the words
the runtime matches on. `configs/model.json` holds provider access. No threshold
and no matchable word is written in code.

## Environment

To see the values in force, including every override, run `opsdeck config`.
It prints the settings, `runtime.json` and `model.json` as JSON; any scalar
under a key that contains `key`, `token`, `secret` or `password` is shown as
`***`. The guardrail catalog is not among them: it is a document rather than a
set of settings, and `guardrails.catalog` says where to read it.

Every variable is prefixed `OPSDECK_`. Relative paths resolve against the
current working directory. `.env.example` is a starting point; nothing in the
package loads a `.env` file, so export it yourself:

```sh
set -a; . ./.env; set +a
```

| Variable | Default | Meaning |
| --- | --- | --- |
| `OPSDECK_SKILLS_ROOTS` | `examples/ops/skills`, `state/skills` | Catalog roots. Separate several with `:` or `,` |
| `OPSDECK_RUNTIME_CONFIG` | `configs/runtime.json` | The runtime configuration file |
| `OPSDECK_MODEL_CONFIG` | `configs/model.json` | The model configuration file |
| `OPSDECK_CONNECTORS_CONFIG` | `examples/ops/configs/connectors.json` | The connector bindings |
| `OPSDECK_SCHEMAS_DIR` | `schemas` | Where the JSON schemas live |
| `OPSDECK_SCENARIOS_DIR` | `examples/ops/scenarios` | Default directory for `opsdeck conform` |
| `OPSDECK_STATE_DIR` | `state` | Where sessions, receipts, audit and proposals are kept |
| `OPSDECK_PROVIDER` | unset | `openai_compatible`, `scripted` or `none`. Overrides `provider` in `model.json` |
| `OPSDECK_MODEL_BASE_URL` | unset | Overrides `base_url` |
| `OPSDECK_MODEL_API_KEY` | unset | Overrides `api_key` |
| `OPSDECK_MODEL_CHAT` | unset | Overrides `chat_model` |
| `OPSDECK_MODEL_EMBEDDING` | unset | Overrides `embedding_model` |

An empty or whitespace-only value is treated as unset. The four model overrides
apply on top of `model.json`, so credentials stay out of the repository while
the model names can live in either.

`--skills` on any command replaces the catalog roots for that run.

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

`languages` changes nothing on its own. It is the list `opsdeck enrich run`
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

The shipped lists carry English and Italian. Add the languages your operators
use; these are also shown to the model router in its prompt, so the two layers
agree on what a yes looks like.

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
| `audit.path` | `state/audit.jsonl` | The audit trail |
| `sessions.path` | `state/sessions.sqlite` | Conversation state, one row per session |
| `learning.publish_root` | `state/skills` | The catalog root a change request is meant to land in |

These are resolved through the state directory: a relative path keeps only its
last segment and is placed under `OPSDECK_STATE_DIR`, an absolute path is used
as it stands. Every other path in this file that names something mutable —
`tracing.path`, `improvement.learned_root`, `improvement.report_path`,
`catalog.revisions_root`, `changes.root` — is resolved the same way. So setting
`OPSDECK_STATE_DIR=/var/lib/opsdeck` moves them all at once, and nothing in
`runtime.json` needs to change.

`learning.publish_root` names a root rather than causing anything to be written
there. Nothing publishes into it on its own: `opsdeck change apply` takes the
root as a required argument, and this is the value a person would usually pass.
The conformance runner is the one caller that reads it, standing in for the
person so a level 3 scenario can see the procedure it published.

### `reply`

| Key | Default | Meaning |
| --- | --- | --- |
| `reply.default_language` | `"en"` | The language a session answers in until a turn settles one |
| `reply.conversation_window` | `6` | How many recent messages the reply writer sees |

The router reports the language of each message, and once a session has one it
keeps it until another turn changes it.

### `api`

| Key | Default | Meaning |
| --- | --- | --- |
| `api.host` | `127.0.0.1` | Address `opsdeck serve` listens on |
| `api.port` | `8000` | Port it listens on |

`--host` and `--port` on `opsdeck serve` override these for one run.

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

Off by default and safe to leave off. Traces are diagnostics: how long each
stretch of a turn took and what it was nested in. They are not the audit
record, nothing reads them back, and no decision is taken from one. Turning
them on costs one appended line per span and a file that grows without bound —
there is no rotation — so it is something to switch on while a question is open
and off again afterwards. A turn that is being traced emits a `trace` audit
event naming where the spans are going, so the audit trail records that a
diagnostic file exists.

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

`enabled` is what the runtime consults, not what `opsdeck enrich run` consults:
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

With no revision built for the tenant, the configured skill roots are read
directly and reload when the files under them change. That is what one process
on its own disk wants and what the example catalogs use; several replicas want
a revision, because a directory tells each of them about a change at a
different moment.

Naming a `revision` pins the deployment to it and takes the choice away from
`opsdeck revision use`, which is right for a rollback under review and wrong as
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
| `changes.root` | `state/changes` | Where a proposed catalog change is written for a person to review, resolved under the state directory |

One directory per request, holding the package laid out the way a catalog root
is laid out, the request record and a summary. Moving it moves where a reviewer
looks; it changes nothing about who may apply one, because `opsdeck change
apply` takes the catalog root as a required argument and there is no configured
value it could fall back to.

### `resilience`

What a connector is allowed to cost before a capability is called unavailable.
See [governance.md](governance.md#when-the-system-does-not-answer).

| Key | Default | Meaning |
| --- | --- | --- |
| `resilience.timeout_seconds` | `30.0` | Seconds one call may take before it counts as never having arrived |
| `resilience.failures_before_open` | `3` | Consecutive calls that never arrived before the binding stops being called |
| `resilience.recovery_seconds` | `30.0` | How long it stays out of service before one attempt is let through |

These apply to every binding the connector registry builds, and only transport
failures count toward them: a system that answered no is a system that is up.

Raising the timeout makes a slow system look healthy and a turn take longer —
it is spent inside the turn, so it competes with everything else the turn has
to do. Lowering `failures_before_open` to one takes a binding out of service on
a single blip; raising it means more turns wait for a system that is already
known to be down. `recovery_seconds` is how quickly a recovered system is
noticed, and the cost of shortening it is one call against a system that may
still be broken.

### `retention`

| Key | Default | Meaning |
| --- | --- | --- |
| `retention.days` | `0` | How long the record of a conversation is kept. Zero keeps everything |

The window is what `opsdeck forget expired` applies when it is given no
`--days`, measured from the moment each session was last written. Nothing
applies it on its own: no turn expires anything, and the serving process
removes nothing, so a deployment that wants a window has to run the command on
a schedule. Deletion on request, `opsdeck forget session`, is answered whatever
this says.

Setting it costs what it sounds like it costs. The receipts, the audit entries
and the stored state of a conversation past the window are gone together, and
the account of what was written goes with them — which is the point, and is
also why the number deserves a decision rather than a default.

### `storage`

| Key | Default | Meaning |
| --- | --- | --- |
| `storage.busy_timeout_seconds` | `10.0` | How long a connection waits for another to finish before it gives up |

The sessions and the ledger are SQLite, and two turns of the same process may
write at once. This is how long one waits for the other rather than failing the
turn. Raising it is worth doing only when turns start failing on a busy file
rather than on the system they were talking to; lowering it turns contention
into a failed turn sooner, which is a diagnosis and not a setting to run with.

## `configs/model.json`

| Key | Default | Meaning |
| --- | --- | --- |
| `provider` | `"none"` | `openai_compatible`, `scripted` or `none` |
| `base_url` | `null` | Endpoint root for the compatible provider |
| `api_key` | `null` | Sent as a bearer token when set |
| `chat_model` | `""` | Model name for completions |
| `embedding_model` | `""` | Model name for embeddings |
| `timeout_seconds` | `30.0` | HTTP timeout |

The file in the repository names no endpoint and no model. It carries
`chat-model-name` and `embedding-model-name`, and `.env.example` carries
`https://your-provider.example/v1`, because the runtime has no preference among
endpoints that speak the protocol. Replace the three with the endpoint and the
model names of the provider you run against.

With `provider: "none"` the runtime routes by rule and by trigger, ranks the
catalog by keyword overlap, answers what it can do, and declines to plan. That
is a working configuration: `validate`, `capabilities`, `ontology` and the
greeting and off-scope paths all work without a provider.

`openai_compatible` speaks the chat-completions protocol. It asks for a
structured response; an endpoint that rejects that field with an HTTP 400 naming
it is retried once with the schema written into the system message, and the
first JSON object in the answer is taken.

`scripted` replays recorded answers. On the command line it needs `--script`
pointing at a file: a JSON list of steps, or an object with a `provider_script`
or `script` key — which is why a scenario file can be handed to it directly.

Keep the key out of the file. `OPSDECK_MODEL_API_KEY` exists for that.

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

### `shell`

| Key | Meaning |
| --- | --- |
| `timeout` | Seconds. Defaults to 30 |
| `operations.<name>.argv` | The argument vector, with `{field}` placeholders |
| `operations.<name>.cwd` | Working directory |
| `operations.<name>.timeout` | Overrides the binding's timeout |

The vector is declared up front and rendered one argument at a time, after each
value is checked. The command is never assembled into a string and never handed
to a shell, so a payload value can only ever become one argument.

### `${ENV}` expansion

`${NAME}` inside an address or a header is replaced by the environment variable
of that name, so `"Authorization": "Bearer ${RUNTIME_TOKEN}"` keeps the token out
of the file. A variable that is not set becomes an empty string and is recorded
in `ConnectorRegistry.warnings`; the binding is still built, and the call fails
at the far end rather than at load time. Expansion applies to `base_url` and to
header values.

`examples/ops/configs/connectors.real.example.json` is the same catalog bound to
an HTTP API and a command line tool. The operation names and payload fields are
the ones the integration skills declare and do not change with the binding,
which is the point: swapping the file swaps the systems, not the catalog.

## The state directory

Everything mutable lives under `OPSDECK_STATE_DIR`, `state/` by default, and
`state/` is in `.gitignore`.

```
state/
  sessions.sqlite     session state: history, principal, revision, pending goal, pending approval, language
  ledger.sqlite       write receipts
  audit.jsonl         one JSON event per line
  traces.jsonl        one span per line, only when tracing is on
  proposals/          one JSON file per proposal, named by proposal id
  changes/            one directory per change request: the package, the request, the summary
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
of the default catalog roots, which is where `opsdeck change apply` is usually
pointed. Nothing under `learned/`, `feedback/` or `preferences/` is authored:
deleting a file undoes what it held and nothing else.

A revision directory is written once and never again, so the contents of
`revisions/` are the only thing here that is safe to copy between machines as
it stands. Deleting one a session is pinned to is a way to break that session.

Delete the directory to start clean. Delete `ledger.sqlite` alone and the
runtime forgets which writes it has already done, which is exactly as dangerous
as it sounds.
