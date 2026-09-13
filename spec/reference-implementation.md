# Reference implementation

`opsdeck` is the reference implementation of OSKA 0.1. `examples/ops` is the
reference conformance domain: a small estate of services, deployments,
incidents and alerts, with the procedures an operator would run against it.

The reference implementation is normative only where this specification points
at it. Where the two disagree, the specification wins.

## Layer to package

| Layer | Packages |
| --- | --- |
| L1 Integration | `src/opsdeck/connectors/` (base, mock, http, shell, registry); operation declarations in `src/opsdeck/contracts/skill.py` |
| L2 Ontology | `src/opsdeck/ontology/` (graph, queries, export); entity and integration skills under `examples/ops/skills/` |
| L3 Skill | `src/opsdeck/catalog/` (loader, registry, validator, resolver); `src/opsdeck/contracts/skill.py` |
| L4 Governance | `src/opsdeck/governance/` (capabilities, policy_engine, approval, verification, ledger); `src/opsdeck/audit/` |
| L5 Cognition | `src/opsdeck/index/`, `src/opsdeck/router/`, `src/opsdeck/planner/` |
| L6 Interaction | `src/opsdeck/engine/`; `src/opsdeck/services.py`; channels in `src/opsdeck/cli/` and `src/opsdeck/api/` |
| L7 Evolution | `src/opsdeck/learning/` (proposals, quality, lifecycle) |
| Contracts | `src/opsdeck/contracts/` |
| Configuration | `src/opsdeck/config.py`, `configs/` |
| Conformance | `src/opsdeck/conformance/` (runner, report); scenarios in `examples/ops/scenarios/` |

`src/opsdeck/services.py` belongs to no layer of its own: it assembles one
engine, one catalogue and one proposal store, and both channels are built over
that single assembly. `src/opsdeck/conformance/` sits outside the layer stack
too: it drives a runtime through recorded sessions and reports the level
reached, so it depends on everything and nothing depends on it.

## Naming differences

The specification uses technology-neutral names. The implementation uses the
names its domain reached for first. They denote the same thing.

| Specification | Implementation |
| --- | --- |
| Procedure | Runbook (`runbook.*`, `SkillType.runbook`, `Plan.runbook_id`) |
| Catalogue | Catalog (`opsdeck.catalog`, `SkillRegistry`) |
| Operation registry | `governance/capabilities.py`, the view over declared operations |
| Ontology graph | `ontology/graph.py` (`OntologyGraph`, `OntologyCache`), with the entity-facing wording in `catalog/resolver.py` |
| Ontology queries | `ontology/queries.py` (`operations_on`, `writers_of`, `procedures_for`, `related`, `path`, `vocabulary`) |
| Operation binding | `OperationSpec.entity` and `OperationSpec.transitions` in `contracts/skill.py` |
| Entity state and states | `state_field` and `states` on the entity frontmatter |
| Model provider | `planner/provider.py`, with a scripted and a service-backed implementation |
| Scenario runner | `opsdeck.conformance` (`ScenarioFile`, `run_scenario`, `run_suite`) |
| Conformance level | `ScenarioFile.level` and `conformance/report.py:attained_level` |

## Where to start reading

1. `examples/ops/skills/integration/container_runtime/v1/skill.md` — what a
   declared operation looks like, including a write with its read-back and its
   idempotency fields.
2. `examples/ops/skills/runbook/service_restart/v1/skill.md` and its
   `policy.json` — a procedure and the policy that governs its write.
3. `src/opsdeck/engine/engine.py` — the turn algorithm of
   [08-interaction-layer.md](08-interaction-layer.md).
4. `src/opsdeck/engine/execution.py` — the write path of
   [06-governance-layer.md](06-governance-layer.md).
5. `examples/ops/scenarios/02_restart_staging.json` — a governed write from
   request to receipt to ledger replay, as an executable document.
6. `examples/ops/scenarios/15_learning_lifecycle.json` — the same session
   turned into a candidate procedure, moved by a named reviewer and published
   into the catalogue, as an executable document.

## Running it

The catalogue is validated and inspected without a model provider:

```
opsdeck validate
opsdeck capabilities
opsdeck ontology
```

The conformance suite runs offline against the scripted provider, and reports
the level it reaches:

```
opsdeck conform
```

`opsdeck conform --json` prints the same report as data, and leaves a non-zero
status when a scenario did not pass. Scenario documents live in
`examples/ops/scenarios/` and are also executed one session per file by
`tests/scenarios/test_scenarios.py`, through the same runner.

## Coverage

Every requirement of the specification carries a module and a proof in the
checklist of [12-conformance.md](12-conformance.md), and there are no open
items. What the checklist does not claim is completeness of the reference
domain: `examples/ops/` is one estate with four integrations, and a second
domain is the next thing that would test the layering rather than the rules.
