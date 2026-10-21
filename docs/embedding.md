# Embedding the runtime in a program

OSKA is a library before it is a server. Everything the command line and the
HTTP application do goes through the same few calls, and a program can make
them directly.

The example below builds the runtime over the example operations catalog and
drives one session with a scripted provider, so it needs no network and no key.
Run it from the repository root. The scripted provider answers from the
`provider_script` of a conformance scenario; that script is what a model would
have said for these three turns.

```python
import json
from pathlib import Path

from oska import Principal, ScriptedModelProvider, build_services, load_settings

scenario = json.loads(Path("examples/ops/scenarios/02_restart_staging.json").read_text())
provider = ScriptedModelProvider(scenario["provider_script"])

settings = load_settings()  # OSKA_STATE_DIR says where state lives; the default is ./state
services = build_services(settings, provider=provider)
engine = services.engine
who = Principal(subject="oncall@example.org", authenticated=True)
session = "embedding-demo"

# 1. The request. The runtime plans, reads, and stops at the write.
request = "the search service in staging needs a restart, it is wedged again"
first = engine.turn(session, request, who)
assert first.pending is not None and first.pending.kind == "approval"
held = first.pending.approval
print("held for approval:", held.operation, held.exact_payload, held.payload_hash)
assert held.exact_payload == {"service": "search"}
assert engine.receipts(session) == []  # nothing has been written yet

# 2. The approval. The exact payload shown above is what runs, then it is read back.
second = engine.turn(session, "yes", who)
receipt = engine.receipts(session)[0]
print("receipt:", receipt.operation, receipt.status, receipt.verification.value)
assert [o.kind for o in second.outcomes] == ["receipt"]
assert receipt.verification.value == "verified"

# 3. The same request again. The ledger recognizes it; nothing runs twice.
third = engine.turn(session, "restart the search service in staging again", who)
print("outcomes:", [o.kind for o in third.outcomes])
assert "ledger_hit" in [o.kind for o in third.outcomes]
assert len(engine.receipts(session)) == 1
print(third.reply)
```

What each call returned:

- `engine.turn(session_id, text, principal)` runs one turn and returns a
  `TurnResult`: `reply` is the text for the person, `outcomes` lists what the
  runtime did (`read`, `approval_pending`, `receipt`, `ledger_hit`, `denied`,
  `rejected`, and so on), `pending` says what the session is waiting for.
- `result.pending.approval.exact_payload` is the payload the write will carry,
  with `payload_hash` as its digest. Approving sends that payload and no other.
- `engine.receipts(session_id)` returns the write receipts of the session,
  oldest first. A receipt is `verified` only when the read-back from the system
  of record showed the change.
- The repeat is a `ledger_hit`: the idempotency key of the write is already in
  the ledger for the session, so the runtime reports the earlier receipt
  instead of writing again.

`build_services` returns the engine together with the proposal and change
request stores a channel needs. A program that only wants turns can call
`build_engine(settings, provider=provider)` from `oska` instead.
`build_platform(manifest, settings)` assembles several domains and tenants from
a platform manifest; see [packs-and-platforms.md](packs-and-platforms.md).

## Replaying a recorded session instead

`ReplayProvider(Path("examples/ops/cassettes/restart-in-italian.json"))` takes
the place of `ScriptedModelProvider` and answers from a cassette recorded
against a real model. That cassette is a session in Italian, so the three
requests are the ones it was recorded with; see
[conformance.md](conformance.md) for recording one of your own.

## Using a real model

Leave out `provider=` and name one in the configuration, or build it yourself.
The model configuration (`configs/model.json`, see
[configuration.md](configuration.md)) says which adapter and which profile to
use, and the keys are read from the environment, never from the file:

```python
from oska import Settings, build_services
from oska.config import load_model_config
from oska.planner.providers import build_provider

settings = Settings.from_env()
provider = build_provider(load_model_config(settings=settings))
services = build_services(settings, provider=provider)
```

With `OSKA_PROVIDER` unset and no provider passed, the engine still routes by
rule and by trigger and answers what it can do; it declines to plan.

## The principal and where state lives

The principal is what your channel authenticated: a `Principal` with a
`subject`, optional `attributes` that procedures can read, and `authenticated`.
The first turn of a session records it; a later turn that arrives under another
identity raises `PrincipalMismatch` instead of running. A procedure that takes
a value from the principal, such as who is asking, refuses to run without an
authenticated one, and a plan that supplies that value itself is refused.

Sessions, ledger receipts, the audit log and proposals live under
`OSKA_STATE_DIR` (default `state/`) with the files backend, or in PostgreSQL
when the runtime configuration selects it; see
[configuration.md](configuration.md). A process that restarts on the same state
continues the same sessions and keeps the same ledger.

## Every write still stops for approval

Embedding does not loosen the write path. A write is planned by the model,
checked by validation, policy and guardrails, held for approval of its exact
payload, executed once, and read back. A program that wants to approve on a
person's behalf passes the person's own answer to `engine.turn`; there is no
call that skips the hold.

## What not to do

Do not call a connector directly from your program. A direct call skips the
catalog's declaration of the operation, policy, the approval of the payload, the
idempotency check, the read-back and the audit record, which is everything the
runtime exists to provide. If an operation is missing, declare it in the
catalog ([skill-contract.md](skill-contract.md)) and reach it through a turn.
