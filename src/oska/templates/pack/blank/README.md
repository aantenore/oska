# ${pack}

A starting pack: one entity, one integration, one read-only procedure, one
governed write. It passes `oska pack check .` as generated. Everything here is
a placeholder for a real business area; replace it piece by piece and re-run
`oska pack check .` after each change.

## Layout

- `pack.yaml` — the manifest. `name`, `version`, `description`, what runtime
  version this pack requires, what it `provides` (checked against the skills
  below), and the `host` identity it answers as. Bump `version` whenever the
  pack's declarations change.
- `skills/entity/${entity}/v1/skill.md` — the `${entity}` record: its fields,
  its identifier, and the lifecycle it moves through (`active` → `archived`).
  Rename the directory and the `id` together; a skill's id and its path always
  agree.
- `skills/integration/${system}/v1/skill.md` — the one binding this pack
  needs: a read (`${entity}.get`) and a governed write (`${entity}.archive`),
  each with its input and output shape. The write declares its own read-back
  in `verify`, which is what proves an archive actually happened.
- `skills/runbook/${entity}_status/v1/skill.md` — reads ${a_entity} back and
  reports its status. No policy file: it writes nothing, so nothing here needs
  approval.
- `skills/runbook/${entity}_archive/v1/skill.md` and its `policy.json` — the
  governed write. The policy says who may run `${entity}.archive` and under
  what condition, and the `guardrails` list is what the runtime holds it to:
  only declared operations, no invented identifiers, the exact approved
  payload, and a read-back before it is ever called done.
- `*/evaluation.json` beside each runbook — the requests that must reach it
  and the requests that must reach the other procedure instead. Run
  `oska evaluate catalog --skills skills` after changing a trigger.
- `configs/connectors.json` — binds `integration.${system}` to the mock
  connector reading `fixtures/${entities}.json`. Point this at a real system
  when the pack is ready; nothing under `skills/` has to change to do that.
- `fixtures/${entities}.json` — the two ${entities} the mock connector serves:
  `demo-1` (active) and `demo-2` (archived). The scenarios below are written
  against these two records.
- `scenarios/01_greeting.json` — level 1: an opening greeting, answered from
  the catalog with no model call.
- `scenarios/02_${entity}_archive.json` — level 2: archiving `demo-1` waits
  for one approval, is approved, and the receipt is verified by a read-back.
  Asking again in the same session replays the receipt from the ledger
  instead of writing a second time.
- `scenarios/03_off_scope.json` — level 1: a request this pack cannot answer
  is declined, and the reply still says what the pack can do.

## What to change first

1. Rename `${entity}` to the real record this pack is about, everywhere the
   name appears: the directory under `skills/entity/`, the `id` inside that
   file, every reference to `entity.${entity}` in the integration and the
   runbooks, and the fixture and connector keys.
2. Replace `integration.${system}` with a binding to the real system: keep
   the operation names and shapes if they still fit, or change them and
   update every runbook and the fixture together.
3. Edit `pack.yaml`'s `description` and `host`, and update `provides` to
   match whatever entities and procedures the pack ends up declaring.
4. Add the next procedure the business area actually needs, following the
   same shape as `${entity}_status` or `${entity}_archive`: a runbook needs
   at least three triggers, a `runtime.action`, and — if it writes — a
   `policy.json` and the guardrails that make the write governed.
5. Run `oska pack check .` after every step. It validates the catalog, runs
   every evaluation set, and runs every scenario, and reports the
   conformance level the pack reaches against the level `pack.yaml` claims.
