# Configuration

Three sources, each with one job. The environment says where things are and
which provider to use. `configs/runtime.json` holds the thresholds and the words
the runtime matches on. `configs/model.json` holds provider access. No threshold
and no matchable word is written in code.

## Environment

To see the values in force, including every override, run `opsdeck config`.
It prints the settings and both configuration files as JSON; any scalar under a
key that contains `key`, `token`, `secret` or `password` is shown as `***`.

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

Only published skills that declare triggers are candidates, so entities and
integrations are never ranked whatever this says. Composites are excluded by
default: they are reached by their triggers on the fast path, or by the model
naming one, rather than competing on a score with the runbooks they contain.

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
| `learning.publish_root` | `state/skills` | Where an approved proposal is published |

These four are resolved through the state directory: a relative path keeps only
its last segment and is placed under `OPSDECK_STATE_DIR`, an absolute path is
used as it stands. So setting `OPSDECK_STATE_DIR=/var/lib/opsdeck` moves all
four at once, and nothing in `runtime.json` needs to change.

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
  sessions.sqlite     session state: history, pending goal, pending approval, language
  ledger.sqlite       write receipts
  audit.jsonl         one JSON event per line
  proposals/          one JSON file per proposal, named by proposal id
  skills/             published proposals, as a catalog root
```

`sessions.sqlite` always sits directly under the state directory; the other
three are placed there by the resolution rule described above. `state/skills` is
one of the default catalog roots, which is how a published proposal reaches the
runtime without anything being restarted.

Delete the directory to start clean. Delete `ledger.sqlite` alone and the
runtime forgets which writes it has already done, which is exactly as dangerous
as it sounds.
