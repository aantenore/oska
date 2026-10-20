# Running it in containers

One image serves the example operations catalog over HTTP, and one compose file
runs it next to PostgreSQL. Both are for trying OSKA on your own machine. The
default image requires no identity and listens on every interface of its
container, so do not put it on a network other people can reach. The last
section says what to change first if you mean to.

What is in the box:

| File | What it is |
| --- | --- |
| `Dockerfile` | Builds the wheel, then installs it with the `api` and `postgres` extras into `python:3.12-slim`. Runs as user `oska` (uid 10001) in `/srv/oska`. |
| `.dockerignore` | Keeps `.env`, `.git`, `state/`, caches, tests and docs out of the build context. |
| `compose.yaml` | The image plus `postgres:16`, with the record kept in PostgreSQL and the API published on `127.0.0.1:8000`. |
| `deploy/compose/runtime.json` | The runtime configuration the compose file mounts: PostgreSQL storage, nothing else. |
| `deploy/compose/runtime.token.json`, `runtime.proxy.json` | The same, with token identity or a proxy secret turned on. |

## Build and run

Image alone, with the files backend under `/srv/oska/state`:

```sh
docker build -t oska:local .
docker run --rm -p 127.0.0.1:8000:8000 -v oska-state:/srv/oska/state oska:local
```

Image and PostgreSQL:

```sh
docker compose up -d --build
docker compose ps        # both services healthy
docker compose logs oska
```

The logs open with the warning `oska serve` prints when it listens on a
non-loopback address while `identity.required` is off:

```
warning: listening on 0.0.0.0:8000 with identity.required off. Anyone who can reach
this port can hold a conversation and ask for writes. ...
```

That is intended here. Inside a container, `127.0.0.1` would be unreachable
from the host, so the process listens on `0.0.0.0` and the port mapping does the
restricting: `compose.yaml` publishes `127.0.0.1:8000:8000`, so only this
machine reaches it.

The container health check calls `GET /health` with the standard library, so
the image carries no `curl`.

## A session with the API

The image carries no model. The commands below are the ones that need none, and
their output was taken from a running `docker compose up`.

```sh
curl -s http://127.0.0.1:8000/health
```

```json
{"status":"ok","skills":15,"fingerprint":"7b682b2cb8faeea89663f54a19d9588f32d6f6b311d707d7a080e0d3eda6249f"}
```

```sh
curl -s -X POST http://127.0.0.1:8000/sessions/demo-1/turns \
  -H 'content-type: application/json' \
  -d '{"text":"restart the search service in staging"}'
```

The response is a `TurnResult`: `reply`, `outcomes`, `pending`, `events` and
`directives`. Trimmed to the reply:

```
Planning a procedure needs a model provider, and none is configured. I can still tell you what
this installation can do. I can check the health of a service, search a service's logs, restart
a service, roll a service back to an earlier version, silence an alert, triage an incident on a
service and add a note to an incident.
```

`events` shows the runtime found the right procedure (`runbook.service_restart`
ranked first, score 0.43) and stopped because planning is the model's job. The
agent proposes and the runtime decides: without a model nothing is proposed, so
nothing is written.

```sh
curl -s http://127.0.0.1:8000/sessions/demo-1/receipts
```

```json
[]
```

The tables are in PostgreSQL, created on start:

```
$ docker compose exec postgres psql -U oska -d oska -c '\dt'
 oska_audit_events, oska_change_request_files, oska_change_requests, oska_migrations,
 oska_proposals, oska_receipts, oska_sessions, oska_write_claims
```

### The approval and the receipt

A write needs a plan, so it needs a model (next section). Once one is
configured, the same two calls produce the exchange below. These responses were
recorded in-process against the same routes with the scripted provider that
replays `examples/ops/scenarios/02_restart_staging.json`, not from the
container, and trimmed (`events`, `directives` and the plan are left out). A real
model words the `reply` differently; the shape does not change.

```sh
curl -s -X POST http://127.0.0.1:8000/sessions/demo-1/turns \
  -H 'content-type: application/json' \
  -d '{"text":"the search service in staging needs a restart, it is wedged again"}'
```

```json
{
  "reply": "This changes something, so it needs your approval first.\nservice.restart on integration.container_runtime with {\"service\":\"search\"}\nReply yes to approve it, or no to leave it alone.",
  "outcomes": [
    {"kind": "read", "operation": "service.status",
     "data": {"ok": true, "data": {"service": {"name": "search", "environment": "staging",
              "status": "running", "version": "0.8.2", "replicas": 2, "restarts": 0}}}},
    {"kind": "approval_pending", "operation": "service.restart",
     "data": {"approval_id": "e0b5584d-75c0-4769-a8cc-5b3bf7fcb9c8",
              "payload": {"service": "search"}, "approvals_required": 1, "approvals_given": 0}}
  ],
  "pending": {
    "kind": "approval",
    "approval": {
      "operation": "service.restart",
      "exact_payload": {"service": "search"},
      "payload_hash": "1eaed93cfd66ea36bbc4a6758d94f3f630698866c436bef6af2c6602a86edbff",
      "approvals_required": 1,
      "approvals_given": 0
    }
  }
}
```

The write has not run. `pending.approval` holds the exact payload and its
digest. Approving is another turn in the same session:

```sh
curl -s -X POST http://127.0.0.1:8000/sessions/demo-1/turns \
  -H 'content-type: application/json' -d '{"text":"yes"}'
```

```json
{
  "reply": "search has been restarted and the read-back shows it running again under the same name, so the receipt is verified.\nservice.restart ran and the read-back confirmed service.name, service.status. Receipt d1211850-7d96-41c6-b14b-4440c8ca2967 is verified.",
  "outcomes": [
    {"kind": "receipt", "operation": "service.restart",
     "receipt_id": "d1211850-7d96-41c6-b14b-4440c8ca2967",
     "data": {"status": "executed", "verification": "verified",
              "evidence": ["service.name", "service.status"]}}
  ],
  "pending": null
}
```

```sh
curl -s http://127.0.0.1:8000/sessions/demo-1/receipts
```

```json
[
  {
    "receipt_id": "d1211850-7d96-41c6-b14b-4440c8ca2967",
    "session_id": "demo-1",
    "integration_id": "integration.container_runtime",
    "operation": "service.restart",
    "payload": {"service": "search"},
    "status": "executed",
    "verification": "verified",
    "verification_evidence": {"comparisons": {
      "service.name": {"expected": "search", "actual": "search", "match": true},
      "service.status": {"expected": "running", "actual": "running", "match": true}}}
  }
]
```

The receipt says `verified` because the read-back did: the runtime read the
service's state again after the restart and compared it with what the procedure
declared. With the compose file the receipt is a row in `oska_receipts`.

The example connectors are the mock one. They read and write a copy of the
fixtures held in memory, so a container restart returns the estate to its
original state. Nothing outside the container is touched.

## Pointing it at a model

Model access is configuration, never part of the image. Set the variables from
`.env.example` in your shell, or in a `.env` next to `compose.yaml` (Compose
reads that file for the `${...}` values; `.gitignore` and `.dockerignore` both
list it):

```sh
export OSKA_PROVIDER=openai_compatible        # or anthropic
export OSKA_MODEL_BASE_URL=https://your-provider.example/v1
export OSKA_MODEL_API_KEY=...
export OSKA_MODEL_CHAT=your-chat-model
export OSKA_MODEL_EMBEDDING=your-embedding-model
docker compose up -d
```

For the image alone, pass them with `-e OSKA_PROVIDER=... -e OSKA_MODEL_API_KEY`
(a name without a value is taken from your shell, so the key is not typed into
the command).
If the provider cannot be built, `serve` stops with one line before it listens;
read it with `docker compose logs oska`. The variables are described in
[configuration.md](configuration.md#configsmodeljson).

## Turning identity on

Identity is a runtime setting, so it comes from a `runtime.json` you mount and a
secret you pass as an environment variable. The compose file mounts whatever
`OSKA_RUNTIME_FILE` names, `deploy/compose/runtime.json` by default.

Token identity, where the runtime verifies a signed session token itself:

```sh
export OSKA_RUNTIME_FILE=./deploy/compose/runtime.token.json
export OSKA_SESSION_TOKEN_KEY="$(openssl rand -hex 32)"
docker compose up -d
```

`identity.required` follows to `true` in this mode, and the startup warning
goes away. A call without a token is refused, and one with a token for its
session is answered:

```
$ curl -s -X POST .../sessions/demo-2/turns -d '{"text":"hello"}' ...
{"error":"identity_required","detail":"this runtime answers only a call with an identity"}   401
$ curl -s -X POST .../sessions/demo-2/turns -H "Authorization: Bearer $TOKEN" ...            200
```

The runtime never issues a token it accepts: whatever authenticates your users
issues them, signed HS256 with the same key, carrying `sub`, `sid` (the session
id the call acts in) and `exp`. For a local try, `oska.governance.identity.mint_token`
signs one; see [governance.md](governance.md#token-mode).

A proxy that authenticates people and sets `X-Principal-Subject`, with a shared
secret so nobody else can set it:

```sh
export OSKA_RUNTIME_FILE=./deploy/compose/runtime.proxy.json
export OSKA_PROXY_SECRET="$(openssl rand -hex 32)"
docker compose up -d
```

`runtime.proxy.json` sets `identity.required` to `true` and
`identity.proxy_secret_env` to `OSKA_PROXY_SECRET`. A call with the principal
header but without `X-Proxy-Secret` is a `401 proxy_unverified`. The proxy must
send the secret on every call.

For the image alone, mount the file and pass the variable:

```sh
docker run --rm -p 127.0.0.1:8000:8000 \
  -v "$PWD/deploy/compose/runtime.token.json:/srv/oska/runtime.json:ro" \
  -e OSKA_RUNTIME_CONFIG=/srv/oska/runtime.json \
  -e OSKA_SESSION_TOKEN_KEY \
  oska:local
```

That file selects PostgreSQL storage, and the image alone has no PostgreSQL to
reach. Use it with `docker compose`, or copy it and delete its `storage` block
to keep the files backend.

## Where state lives

| Run | Where |
| --- | --- |
| `docker run` | Sessions, ledger and audit in `/srv/oska/state` (`sessions.sqlite`, `audit.jsonl`, and `ledger.sqlite` once a write has run), a declared volume owned by `oska`. Mount a named volume to keep it. |
| `docker compose` | Ledger, receipts, sessions, audit, proposals and change requests in the `postgres-data` volume. The `oska-state` volume holds the remaining files under `/srv/oska/state`. |

`docker compose down` keeps both volumes. `docker compose down -v` deletes them
and with them every session and receipt.

The database address is read from `OSKA_DATABASE_URL`, which the compose file
builds from `POSTGRES_PASSWORD` (default `oska-local`, for local use only). It
is not in `runtime.json`, because an address carries a credential.

## Before you expose it

The defaults are for one machine. To serve anyone else:

- Turn identity on (above) and keep `identity.required` true.
- Set `POSTGRES_PASSWORD` to a real secret, and do not publish the PostgreSQL port.
- Put TLS and the authenticating proxy in front; change the published address
  from `127.0.0.1` only behind them.
- Bind the example catalog to your own systems instead of the mock connector:
  see [building-a-platform.md](building-a-platform.md) and set
  `OSKA_SKILLS_ROOTS` and `OSKA_CONNECTORS_CONFIG` to mounted paths.
- Read [governance.md](governance.md#identity) for who may do what over HTTP.
