# ${platform}

A generated platform repository. It bundles one or more packs under `packs/`,
names them in `platform.yaml`, and is ready to check as it stands.

## Layout

- `platform.yaml` — what this organization deploys: the packs under `packs/`,
  grouped into domains, each with the host identity it answers as. Generated
  from the packs given to `oska new platform`; edit the `domains` list to
  split packs across more than one domain, or to add tenants.
- `packs/<name>/` — one directory per pack, each a self-contained catalog
  with its own skills, connectors, fixtures, scenarios and evaluation sets.
  See the `README.md` inside a pack for what to change first.
- `configs/runtime.json` — thresholds and copy, copied from the defaults the
  installed runtime ships when this platform was generated. Read by every
  domain unless a tenant overlay changes a value `oska.platform.tenants`
  allows a tenant to change. Delete a file under `configs/` to fall back to
  the packaged default; `oska config` names the one in force under `sources`.
- `configs/model.json` — model provider access. `OSKA_PROVIDER=none` by
  default, so this platform runs offline until a provider is configured
  through `.env`.
- `configs/guardrails.json` — the platform's guardrail catalog, copied from
  the runtime's defaults. A pack that ships its own `guardrails.json` adds to
  this rather than replacing it; an id defined twice is refused.
- The JSON schemas the catalog is checked against ship with the runtime and
  are not copied here; set `OSKA_SCHEMAS_DIR` only to use schemas of your own.
- `.env.example` — copy to `.env` and fill in what your deployment needs;
  `.env` is never committed.
- `.github/workflows/conformance.yml` — runs `oska pack check` for every pack
  under `packs/`, then `oska conform --platform platform.yaml`, on every push
  and pull request.
- `.gitignore` — keeps `state/`, `.venv/` and `.env` out of version control.

## Who can talk to it

This platform starts as a single-user deployment. `configs/runtime.json`, as
generated, is the runtime's own packaged default, copied here unchanged: it
reads identity in `proxy` mode, with `identity.required` off and
`identity.single_user` set to `true`. In plain terms:

- Anyone who can reach the port `oska serve` listens on is this deployment's
  one user, reviewer included. The runtime checks no identity itself: it
  takes the `X-Principal-Subject` and `X-Principal-Attributes` headers as they
  arrive, and a call without them is served too — and because
  `identity.single_user` is `true`, that caller also reads and moves
  proposals and change requests, the work `identity.reviewer_role` would
  otherwise gate. Only a proxy in front of the port that authenticates
  people, and sets those headers itself, makes any of it mean something more
  than "whoever has the port."
- `api.host` is `127.0.0.1`, so only this machine can reach the port as
  generated. `oska serve` prints a warning on stderr whenever it is told to
  listen on any other address while `identity.required` is still off.

Before this deployment serves more than one person:

1. Turn on real identity: in `configs/runtime.json`, either set
   `"identity": {"required": true}` behind a proxy that authenticates people
   and sets `X-Principal-Subject` (and, for a reviewer, `X-Principal-Attributes`
   with a `roles` list holding `identity.reviewer_role`) — or switch to token
   identity, below, where `identity.required` turns on by itself.
2. Remove `identity.single_user` (or set it `false`). It is what makes an
   unauthenticated caller today's one user; once removed, only a principal
   whose `roles` hold `identity.reviewer_role` reads or moves proposals and
   change requests, and everyone else needs an identity too where
   `identity.required` is on.
3. Give each reviewer that role: `identity.reviewer_role` (`"reviewer"` by
   default) named in the `roles` the proxy sets or the token carries.
4. Leave `metrics.public` off unless the caller scraping `GET /metrics` is on
   a private network and presents no identity — turning it on is a statement
   about that network, not about the runtime, so set it only there.

To have the runtime check who is calling, switch on token identity:

1. In `configs/runtime.json`, remove `identity.single_user` (or set it
   `false`) and set `"identity": {"mode": "token"}`. The two are refused
   together — a deployment that verifies tokens is not the deployment of one
   unauthenticated user. In `token` mode `identity.required` turns on by
   itself, so a call without a token is refused — `GET /health` stays open
   regardless, and `GET /metrics` only where `metrics.public` says so.
2. Put a long random value in `.env` as `OSKA_SESSION_TOKEN_KEY`, the
   variable `identity.key_env` names.
3. Have whatever authenticates people issue each of them an HS256 token signed
   with that key, carrying the `sub`, `sid` and `exp` claims (and a `roles`
   list holding the reviewer role for reviewers), sent as
   `Authorization: Bearer <token>`. A token is good for the one session its
   `sid` names.

## From zero to a first governed write

1. `python -m venv .venv && .venv/bin/pip install "${OSKA_INSTALL:-oska==${oska_version}}"`
   installs the runtime version that generated this platform. Set
   `OSKA_INSTALL` (here, or as a repository variable for the CI workflow) to a
   wheel, an index requirement or a git URL to install from somewhere else.
2. `cp .env.example .env` and fill in a model provider, or leave
   `OSKA_PROVIDER=none` to run with the deterministic keyword router only.
3. If this platform was generated without a pack, write one from this
   directory: `.venv/bin/oska new pack ${pack} --into packs`. Written into
   `packs/` beside `platform.yaml`, the pack is named in `platform.yaml` and
   listed in its default domain by the same command, which says so; nothing
   needs editing by hand. Name your own pack in place of `${pack}` in every
   command below.
4. `.venv/bin/oska pack check packs/${pack}` — validates the pack, runs its
   evaluation sets, runs its scenarios, and reports the conformance level it
   reaches.
5. `.venv/bin/oska conform --platform platform.yaml` — the same check across
   every domain this platform declares.
6. `.venv/bin/oska run --skills packs/${pack}/skills --connectors packs/${pack}/configs/connectors.json`
   to hold a conversation with the pack directly from a terminal.
7. Point `packs/${pack}/configs/connectors.json` at a real system in place of
   the mock connector, keeping the operation names and shapes the skills
   declare. The first write that goes out under an approval and comes back
   verified is the platform's first governed write.
8. Commit `platform.yaml`, `packs/`, and `configs/`; never commit `.env` or
   `state/`.
