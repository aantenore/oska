# Reference implementation

`opsdeck` is the reference implementation of OSKA 0.1. There are two reference
conformance domains. `examples/ops` is a small estate of services, deployments,
incidents and alerts, with the procedures an operator would run against it;
`examples/access` is an access management domain of accounts, groups and
requests, sharing no vocabulary with the first.

The reference implementation is normative only where this specification points
at it. Where the two disagree, the specification wins.

## Layer to package

| Layer | Packages |
| --- | --- |
| L1 Integration | `src/opsdeck/connectors/` (base, mock, http, shell, registry); operation declarations in `src/opsdeck/contracts/skill.py` |
| L2 Ontology | `src/opsdeck/ontology/` (graph, queries, export); entity and integration skills under `examples/ops/skills/` |
| L3 Skill | `src/opsdeck/catalog/` (loader, registry, validator, resolver, enrichment); `src/opsdeck/contracts/skill.py` |
| L4 Governance | `src/opsdeck/governance/` (capabilities, policy_engine, approval, verification, ledger, guardrails); `configs/guardrails.json`; `src/opsdeck/audit/` (events, sink, trace) |
| L5 Cognition | `src/opsdeck/index/`, `src/opsdeck/router/`, `src/opsdeck/planner/`; the routing examples they read are derived by `src/opsdeck/catalog/enrichment.py` at publish time |
| L6 Interaction | `src/opsdeck/engine/`; `src/opsdeck/services.py`; channels in `src/opsdeck/cli/` and `src/opsdeck/api/` |
| L7 Evolution | `src/opsdeck/learning/` (proposals, quality, lifecycle); `src/opsdeck/improvement/` (signals, learned, tuning, evaluation, gaps, job, report) |
| Contracts | `src/opsdeck/contracts/` |
| Configuration | `src/opsdeck/config.py`, `configs/` |
| Conformance | `src/opsdeck/conformance/` (runner, report); scenarios in `examples/ops/scenarios/` and `examples/access/scenarios/` |

`src/opsdeck/catalog/enrichment.py` sits in L3 although what it produces is
read by L5: the derived phrases are written beside the skill file they belong
to, at publish time, and L5 only reads them. `src/opsdeck/audit/trace.py` sits
beside the audit sinks and is not one of them; a trace is diagnostic and no
decision is taken from it. `src/opsdeck/improvement/` is in L7 and reaches only
the audit trail, the write ledger and the proposal store: it imports neither
the lifecycle nor a connector nor the policy engine, which is what closes the
rungs above the one it declares.

`src/opsdeck/services.py` belongs to no layer of its own: it assembles one
engine, one catalog and one proposal store, and both channels are built over
that single assembly. `src/opsdeck/conformance/` sits outside the layer stack
too: it drives a runtime through recorded sessions and reports the level
reached, so it depends on everything and nothing depends on it.

## Naming differences

The specification uses technology-neutral names. The implementation uses the
names its domain reached for first. They denote the same thing.

| Specification | Implementation |
| --- | --- |
| Procedure | Runbook (`runbook.*`, `SkillType.runbook`, `Plan.runbook_id`) |
| Catalog | Catalog (`opsdeck.catalog`, `SkillRegistry`) |
| Operation registry | `governance/capabilities.py`, the view over declared operations |
| Ontology graph | `ontology/graph.py` (`OntologyGraph`, `OntologyCache`), with the entity-facing wording in `catalog/resolver.py` |
| Ontology queries | `ontology/queries.py` (`operations_on`, `writers_of`, `procedures_for`, `related`, `path`, `vocabulary`) |
| Operation binding | `OperationSpec.entity` and `OperationSpec.transitions` in `contracts/skill.py` |
| Entity state and states | `state_field` and `states` on the entity frontmatter |
| Entity lifecycle | `lifecycle`, `initial_state`, `final_states` and `state_read` on the entity frontmatter; `LifecycleTransition` in `contracts/skill.py` |
| Lifecycle move | `LifecycleTransition`, and `move_for` / `legal_operations` in `governance/capabilities.py` |
| Guardrail catalog | `governance/guardrails.py` (`GuardrailCatalog`, `GuardrailChain`), over `configs/guardrails.json` |
| Routing example | Derived phrase (`catalog/enrichment.py`, written as `generated-examples.json` beside the skill) |
| Per-turn model budget | `ModelBudget` and `BudgetedProvider` in `engine/engine.py`, wrapped once in `engine/factory.py` |
| Autonomy ladder | `src/opsdeck/improvement/` and the level it renders in `improvement/report.py` |
| Learned phrasing | `improvement/learned.py`, written under the state directory, never into a skill |
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
   into the catalog, as an executable document.

## Running it

The catalog is validated and inspected without a model provider:

```
opsdeck validate
opsdeck capabilities
opsdeck ontology
```

Two commands need a model, and neither of them runs inside a turn.
`opsdeck enrich run` derives the routing examples of every procedure that is
missing them and writes them beside the skill file; `opsdeck enrich run
--dry-run` reports what a pass would write and writes nothing, and
`opsdeck enrich show <procedure>` prints the declared and the derived phrases
of one procedure. `opsdeck improve run` reads the audit trail and the write
ledger, learns the phrasings that are reversible, tunes the routing thresholds
inside their declared bounds, evaluates the labeled set and reports the gaps;
`opsdeck improve show`, `opsdeck improve evaluate` and `opsdeck improve revert
<procedure>` read the last report, re-run the evaluation without changing
anything, and delete what was learned for one procedure. Nothing either command
does moves a proposal past `draft`.

```
opsdeck enrich run
opsdeck improve run
```

The conformance suite runs offline against the scripted provider, and reports
the level it reaches. There are two suites, one per reference catalog, and
each is run against its own catalog and its own connector bindings:

```
opsdeck conform

opsdeck conform \
  --skills examples/access/skills \
  --connectors examples/access/configs/connectors.json \
  --scenarios examples/access/scenarios
```

The first invocation takes the operational domain from the default settings.
`opsdeck conform --json` prints the same report as data, and leaves a non-zero
status when a scenario did not pass. Scenario documents live in
`examples/ops/scenarios/` and `examples/access/scenarios/`, and are also
executed one session per file by `tests/scenarios/test_scenarios.py`, through
the same runner.

## Coverage

Every requirement of the specification carries a module and a proof in the
checklist of [12-conformance.md](12-conformance.md). The requirements whose
proof is still `pending` are listed there under "Open items", with the reason
in each case. What the checklist does not claim is completeness of either
reference domain: between them they declare seven integrations, which is enough
to exercise the layering and not enough to stand in for an estate.
