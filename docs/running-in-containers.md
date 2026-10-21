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
| `compose.yaml` | The image plus `postgres:16`, with the record kept in PostgreSQL, the API published on `127.0.0.1:8000`, and the recorded session answering by default (`OSKA_DEMO_REPLAY`). |
| `deploy/compose/runtime.json` | The runtime configuration the compose file mounts: PostgreSQL storage, with the default discovery and routing kept, since a runtime file replaces the defaults whole. |
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

followed by the line that says the stack is the demo (next section). The
warning is intended here. Inside a container, `127.0.0.1` would be unreachable
from the host, so the process listens on `0.0.0.0` and the port mapping does the
restricting: `compose.yaml` publishes `127.0.0.1:8000:8000`, so only this
machine reaches it.

The container health check calls `GET /health` with the standard library, so
the image carries no `curl`.

## A session with the API

`compose.yaml` sets `OSKA_DEMO_REPLAY`, so the stack serves
`oska serve --replay examples/ops/cassettes/restart-in-italian.json`: a session
recorded against a hosted model, answered from the recording. Nothing reaches a
model and no key is needed. The process says so before it listens, after the
open-port warning:

```
demo: answering from the recorded session in examples/ops/cassettes/restart-in-italian.json; nothing reaches a model
```

and `/health` says so to anyone who asks:

```sh
curl -s http://127.0.0.1:8000/health
```

```json
{"status":"ok","skills":15,"fingerprint":"7b682b2cb8faeea89663f54a19d9588f32d6f6b311d707d7a080e0d3eda6249f","provider":"replay"}
```

The replay answers by the request, not by its place in the file: each call is
matched to the recorded call that asked the same thing, so several sessions can
run the conversation at once and each gets its own answers. Everything else is
the real runtime: the catalog, the policy, the approval gate, the mock
connector, the read-back, the ledger and PostgreSQL. A receipt id the recorded
answer quoted is replaced by this run's.

The output below is from `docker compose up -d --build` on this repository,
trimmed: `events`, `directives` and the plan are left out of the turns.

Ask for the restart, in Italian:

```sh
curl -s -X POST http://127.0.0.1:8000/sessions/demo-1/turns \
  -H 'content-type: application/json' \
  -d '{"text":"Il servizio search in staging è bloccato, riavvialo per favore"}'
```

```json
{
  "reply": "Questa operazione modifica qualcosa, quindi serve prima la tua approvazione.\nservice.restart su integration.container_runtime con {\"service\":\"search\"}\nRispondi sì per approvarla, oppure no per lasciare tutto com'è.",
  "outcomes": [
    {"kind": "read", "operation": "service.status",
     "data": {"ok": true, "data": {"service": {"name": "search", "environment": "staging",
              "status": "running", "version": "0.8.2", "replicas": 2, "restarts": 0}}}},
    {"kind": "approval_pending", "operation": "service.restart",
     "data": {"approval_id": "e8344b14-33ed-4807-97a3-fa3d16f24ded",
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

The status read ran; the write has not. `pending.approval` holds the exact
payload and its digest. Approving is another turn in the same session:

```sh
curl -s -X POST http://127.0.0.1:8000/sessions/demo-1/turns \
  -H 'content-type: application/json' -d '{"text":"sì"}'
```

```json
{
  "reply": "Il riavvio del servizio search in staging è stato eseguito e verificato. Il servizio è ora in esecuzione con due repliche.\nL'operazione service.restart è stata eseguita e la rilettura ha confermato service.name, service.status. La ricevuta 9b1c2094-5cc0-4c6f-a36d-6c6c842ef013 è verificata.",
  "outcomes": [
    {"kind": "receipt", "operation": "service.restart",
     "receipt_id": "9b1c2094-5cc0-4c6f-a36d-6c6c842ef013",
     "data": {"status": "executed", "verification": "verified",
              "evidence": ["service.name", "service.status"], "payload": {"service": "search"}}},
    {"kind": "read", "operation": "service.status",
     "data": {"ok": true, "data": {"service": {"name": "search", "status": "running",
              "replicas": 2, "restarts": 1}}}}
  ],
  "pending": null
}
```

The receipt says `verified` because the read-back did: the runtime read the
service again after the restart and compared it with what the procedure
declared. Ask for the same thing again:

```sh
curl -s -X POST http://127.0.0.1:8000/sessions/demo-1/turns \
  -H 'content-type: application/json' \
  -d '{"text":"Il servizio search in staging è bloccato, riavvialo per favore"}'
```

```json
{
  "reply": "Il riavvio del servizio search è stato eseguito e verificato. L'operazione è confermata con la ricevuta 9b1c2094-5cc0-4c6f-a36d-6c6c842ef013.\nL'operazione service.restart era già stata eseguita in questa sessione, ricevuta 9b1c2094-5cc0-4c6f-a36d-6c6c842ef013.",
  "outcomes": [
    {"kind": "ledger_hit", "operation": "service.restart",
     "receipt_id": "9b1c2094-5cc0-4c6f-a36d-6c6c842ef013",
     "data": {"status": "executed", "verification": "verified", "payload": {"service": "search"}}}
  ],
  "pending": null
}
```

The repeat is a `ledger_hit`: the ledger already holds this write, so it is not
sent twice and nothing asks for approval again. The session holds one receipt:

```sh
curl -s http://127.0.0.1:8000/sessions/demo-1/receipts
```

```json
[
  {
    "receipt_id": "9b1c2094-5cc0-4c6f-a36d-6c6c842ef013",
    "session_id": "demo-1",
    "ledger_key": "d5e1598688113c3ad962edc433da2847d51fa5a94a7bf4946e8b748b563640f1",
    "integration_id": "integration.container_runtime",
    "operation": "service.restart",
    "payload": {"service": "search"},
    "status": "executed",
    "verification": "verified",
    "verification_evidence": {"operation": "service.status", "ok": true, "comparisons": {
      "service.name": {"found": true, "match": true, "actual": "search", "expected": "search"},
      "service.status": {"found": true, "match": true, "actual": "running", "expected": "running"}}},
    "created_at": "2026-10-21T00:12:01.232414Z"
  }
]
```

It is a row in PostgreSQL:

```
$ docker compose exec postgres psql -U oska -d oska \
    -c "select receipt_id, session_id, operation, verification from oska_receipts"
              receipt_id              | session_id |    operation    | verification
--------------------------------------+------------+-----------------+--------------
 9b1c2094-5cc0-4c6f-a36d-6c6c842ef013 | demo-1     | service.restart | verified
(1 row)
```

A request the recording does not hold gets the answer the runtime gives
without a model, never an invented one:

```sh
curl -s -X POST http://127.0.0.1:8000/sessions/demo-2/turns \
  -H 'content-type: application/json' -d '{"text":"che tempo fa a Roma domani?"}'
```

```
That is not something I can do here. I can check the health of a service, search a service's logs,
restart a service, roll a service back to an earlier version, silence an alert, triage an incident
on a service and add a note to an incident.
```

The example connectors are the mock one. They read and write a copy of the
fixtures held in memory, so a container restart returns the estate to its
original state. Nothing outside the container is touched.

The image alone does not replay: without `OSKA_DEMO_REPLAY` it serves the model
the `OSKA_PROVIDER` variables describe, or none (`"provider":"none"` on
`/health`). To replay with it, pass the variable:

```sh
docker run --rm -p 127.0.0.1:8000:8000 \
  -e OSKA_DEMO_REPLAY=examples/ops/cassettes/restart-in-italian.json oska:local
```

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
export OSKA_DEMO_REPLAY=                      # empty: no replay
docker compose up -d
```

The replay and a model are one or the other. With `OSKA_DEMO_REPLAY` still set,
`serve` stops with one line before it listens:

```
--replay answers from a recording and the configuration names the openai_compatible provider; set OSKA_PROVIDER=none or drop --replay
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
