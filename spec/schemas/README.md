# Normative schemas

Nine machine-readable schemas are normative for OSKA 0.1. Three of them
already live in the repository root, because the runtime validates against them
at load time. Six are defined here: two describe the manifests a deployment
writes (a pack and a platform), one describes a conformance scenario, one a
configured artifact the runtime reads at start-up but does not carry in its
catalog, and two describe records the runtime produces rather than reads.

A test holds each of the six to the code, so none of them can drift
silently: `tests/unit/test_spec_schemas.py`. The pack and platform schemas must
equal what their contracts generate; the scenarios and the guardrail catalog
the repository ships must validate; the audit schema must allow every event
kind the code can emit and the events a real turn writes; and a plan the code
builds must validate once its one renamed field is mapped. The three schemas of
the repository root are held to their packaged copies by
`tests/unit/test_packaged_defaults.py`.

## In the repository root

These are the schemas the reference implementation loads. They are normative
where this specification points at them; this directory does not duplicate
them.

| Schema | Path | Governs |
| --- | --- | --- |
| Skill | `schemas/skill.schema.json` | The frontmatter of every skill artifact: entity, integration, procedure and composite. See [05-skill-layer.md](../05-skill-layer.md). |
| Policy | `schemas/policy.schema.json` | A policy document: ordered rules with effects, conditions and session limits. See [06-governance-layer.md](../06-governance-layer.md). |
| Proposal | `schemas/proposal.schema.json` | A proposal, its state, its quality report and its transition history. See [09-evolution-layer.md](../09-evolution-layer.md). |

A conformant implementation MUST validate every skill artifact against the
skill schema and every policy document against the policy schema before serving
a catalog.

## In this directory

| Schema | File | Governs |
| --- | --- | --- |
| Pack | [`pack.schema.json`](pack.schema.json) | The manifest of one pack, `pack.yaml`: its name, version, requirements, dependencies, what it provides, its host and its synonyms. Generated from `PackManifest`. See [14-platform-blueprint.md](../14-platform-blueprint.md). |
| Platform | [`platform.schema.json`](platform.schema.json) | The manifest of one platform: the packs it deploys, the domains it answers in, its tenants, channels and storage. Generated from `PlatformManifest`. See [14-platform-blueprint.md](../14-platform-blueprint.md). |
| Scenario | [`scenario.schema.json`](scenario.schema.json) | A conformance scenario: a recorded session, a provider script, per-turn expectations, the level it belongs to and an optional proposal lifecycle. See [12-conformance.md](../12-conformance.md). |
| Audit event | [`audit-event.schema.json`](audit-event.schema.json) | One typed record of one step of one turn. See [06-governance-layer.md](../06-governance-layer.md) and [10-cross-cutting.md](../10-cross-cutting.md). |
| Plan | [`plan.schema.json`](plan.schema.json) | The typed plan cognition produces and interaction executes. See [07-cognition-layer.md](../07-cognition-layer.md). |
| Guardrail catalog | [`guardrail-catalog.schema.json`](guardrail-catalog.schema.json) | The declared guardrails of one installation, each naming the point it acts at and which of the three mechanisms keeps it. See [06-governance-layer.md](../06-governance-layer.md). |

## Notes on each

**Pack and platform.** Both files are the JSON Schema their pydantic contract
(`src/oska/contracts/platform.py`) produces, with `$schema` and `$id` added. They
are regenerated, never edited by hand; the test fails when the committed file
and the contract differ.

**Scenario.** The `expect` object of a turn is the contract a runner checks:
the route kind, the fragments the reply must contain, the outcome kinds in
order, what the session waits for, the receipt count and the verification
status of the most recent receipt. `connector_behaviors` belongs beside
`expect`, on the turn, and is applied before the turn runs; the schema accepts
it nowhere else. The `level` field is optional and defaults to 2, and every
reference scenario stamps it. The `learning` block is optional, runs after the
last turn, and turns the session into a candidate procedure a named actor moves
through the lifecycle of [09-evolution-layer.md](../09-evolution-layer.md); a
scenario carrying one belongs to level 3. See
[12-conformance.md](../12-conformance.md) for what a runner must check.

**Audit event.** The envelope — kind, session, turn, timestamp — is closed. The
`data` object is open, because each kind carries its own fields and an
implementation may record more. The conventional keys per kind are listed in
the schema so a reader of an audit file knows what to expect. This schema is
consistent with the event contract of the reference implementation
(`src/oska/contracts/events.py`): the `kind` list is that file's `EventKind`
and the test fails when one has a kind the other lacks.

**Plan.** The schema uses `procedure_id` where the reference implementation
uses `runbook_id`; the two denote the same field. The reference implementation's
plan also carries the `language` the planner detected, which this schema does
not; the test maps both differences before it validates. A plan that validates against
this schema is still not a valid plan: it must additionally name only
operations declared in the procedure's hydrated closure, mark writes exactly as
the declaration does, list every write in the procedure's capabilities, and
carry payloads that satisfy each operation's own input schema. Those checks
belong to the cognition layer and cannot be expressed here, because they depend
on the catalog. See `OSKA-COG-7`.

**Guardrail catalog.** A guardrail catalog is a configured artifact, not a
catalog artifact: it is a document an operator points the runtime at, next to
the connector bindings and the policy documents, and it is not a skill and not
loaded from a catalog root. In the reference implementation it is
`configs/guardrails.json`, named under `guardrails.catalog` in the runtime
configuration. Every entry declares its `point` and its `mechanism`; a `check`
MUST name a validator the runtime registers, MUST be declared only at a point
the guardrail chain runs — `input` or `output` — and MUST carry in `parameters`
every value that validator reads. An `invariant`, which is a promise a named
module keeps where the chain runs nothing, and an `instruction`, which is text
handed to a model, MUST name neither a validator nor parameters. Those rules
are expressed in the schema, so a catalog that lets a sentence handed to a
model, or a rule kept somewhere else, look like a control at the point it is
declared does not validate. See `OSKA-GOV-15` to `OSKA-GOV-20`.

## Identifiers and versioning

Each schema carries an `$id` under `https://oska.spec/schemas/`. The identifier
is stable; the content is versioned with this specification. A field added in a
later revision will be optional, so a document valid against 0.1 stays valid.
