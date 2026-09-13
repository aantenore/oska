# 12 Conformance

Conformance is demonstrated by running a scenario suite, not by inspection. A
system claiming a level MUST publish the suite it ran and the checklist below,
filled in.

## Levels

**Level 1 — Declared.** The system declares what exists and what can be done,
and speaks from its own catalogue.

Requires: all `OSKA-INT-*`, `OSKA-ONT-*` and `OSKA-SKL-*`; `OSKA-COG-11`;
`OSKA-XC-1` to `OSKA-XC-4` and `OSKA-XC-9`. The validator passes over the whole
catalogue with no issues. Every sentence the system says about its capability is
derived. The suite runs offline against a scripted provider. Scenario
categories C1 and C13 pass.

**Level 2 — Governed.** Every effect on the world crosses the write path.

Requires: Level 1, plus all `OSKA-GOV-*`, all `OSKA-IXN-*`, all remaining
`OSKA-COG-*` and all remaining `OSKA-XC-*`. Scenario categories C1 to C14 and
C17 pass.

**Level 3 — Adaptive.** The catalogue grows through a human-controlled
lifecycle.

Requires: Level 2, plus all `OSKA-EVO-*`. Scenario categories C15 and C16 pass.

A system MUST NOT claim a level whose scenario categories it does not run. A
system MAY claim Level 2 without Level 3; evolution is optional, governance is
not.

## Scenario format

A scenario is one session, recorded as a JSON document. The normative schema is
`spec/schemas/scenario.schema.json`.

```json
{
  "name": "02_restart_staging",
  "description": "A governed write: approval, verification, and a ledger replay.",
  "level": 2,
  "provider_script": [
    {
      "purpose": "plan",
      "expect_contains": "needs a restart",
      "response": { "procedure_id": "…", "operations": [] }
    }
  ],
  "turns": [
    {
      "user": "the search service needs a restart",
      "expect": {
        "kind": "new_task",
        "contains": ["approval", "service.restart"],
        "outcomes": ["read", "approval_pending"],
        "pending": "approval",
        "receipts": 0,
        "verification": null
      },
      "connector_behaviours": {
        "integration.container_runtime": { "service.restart": { "fail": true } }
      }
    }
  ]
}
```

Field meanings:

| Field | Meaning |
| --- | --- |
| `name` | Scenario identifier; also the session identifier for the run |
| `description` | What the scenario proves, in one sentence |
| `level` | Lowest conformance level this scenario belongs to; default 2 |
| `provider_script` | Model responses, consumed in order, one per model call |
| `provider_script[].expect_contains` | Substring the prompt reaching this step MUST contain |
| `provider_script[].purpose` | Which call this step answers, for failure messages |
| `turns[].user` | The user's text for this turn |
| `turns[].expect.kind` | The route kind the turn MUST produce |
| `turns[].expect.contains` | Fragments the reply MUST contain |
| `turns[].expect.outcomes` | The outcome kinds, in order, exactly |
| `turns[].expect.pending` | `"input"`, `"approval"` or `null` |
| `turns[].expect.receipts` | Number of receipts in the session after the turn |
| `turns[].expect.verification` | Verification status of the most recent receipt, or `null` |
| `turns[].connector_behaviours` | Failures or overrides injected before the turn runs |

`level` is the lowest level the scenario belongs to, and a scenario carrying no
`level` is read as level 2. A runner MUST report, per level, how many scenarios
of that level passed, and MUST report as attained the highest level whose
scenarios and those of every level below it all pass.

`connector_behaviours` is written beside `expect`, on the turn. Behaviours are
keyed by integration identifier and then by operation name, and are applied
before the turn runs; a null behaviour clears the one in force. That is the
only placement, and a runner MUST NOT read the object from anywhere else.

### The learning block

A Level 3 scenario MAY carry a `learning` block. It runs once, after the last
turn, against the session those turns built: a candidate procedure is drafted
from what the session actually executed, moved through the states the block
names, and checked where it landed.

```json
{
  "learning": {
    "draft_from_session": true,
    "provider_script": [
      { "purpose": "drafting", "expect_contains": "…", "response": { "…": "…" } }
    ],
    "expect_refused": { "to": "submitted", "by": "" },
    "transitions": [
      { "to": "submitted", "by": "s.reyes", "note": "recorded from one session" },
      { "to": "approved", "by": "s.reyes", "note": "wording and policy read" },
      { "to": "published", "by": "s.reyes", "note": "published as a new version" }
    ],
    "expect": {
      "state": "published",
      "published_id": "runbook.service_restart",
      "quality_passed": true
    }
  }
}
```

| Field | Meaning |
| --- | --- |
| `learning.draft_from_session` | Whether the candidate is drafted from the session the turns built; default `true`, and a runner MAY refuse any other source |
| `learning.provider_script` | Model responses for the drafting, spent by a provider of its own, so the wording of the candidate is scripted separately from the turns; without one the candidate is built from the recorded operations alone |
| `learning.expect_refused` | A transition that MUST be refused: `to` names the state, `by` names the actor and defaults to the empty string, the usual case |
| `learning.transitions` | The moves to perform in order, each carrying `to`, `by` and an optional `note` |
| `learning.expect.state` | The state the proposal MUST be in when the block finishes |
| `learning.expect.published_id` | The skill identifier the catalogue MUST hold, published, after the block; unset means the check is not made |
| `learning.expect.quality_passed` | Whether the quality report MUST pass; unset means the check is not made |

The order is normative. `expect_refused` is attempted first, while the proposal
is still a draft, so what refuses it is the missing actor and not an edge that
leads nowhere; a refused move MUST leave the stored state unchanged, and a move
that is allowed where the scenario expects a refusal is a failure. The
transitions are then applied in order, and the first one that is refused stops
the block and is reported.

When `published_id` is set, the runner MUST check three things: the catalogue
holds that identifier, it holds it as published, and the file it holds is the
one under the publication root — so a scenario cannot pass by finding the
hand-written copy the candidate was drafted from. This requires the publication
root to be one of the catalogue roots of the run, which a runner MUST arrange
for a scenario carrying a learning block.

### What a runner must do

A conformant implementation MUST provide a runner that executes a directory of
scenarios and reports pass or fail per scenario. The runner MUST also assert
that the provider script was fully consumed, and that the model calls occurred
in the recorded order — a classification step must have been consumed by a
classification call, not by a planning call. A drafting script, where one is
given, MUST be fully consumed as well.

A runner MUST NOT stop at the first failure: it MUST collect every expectation
that did not hold, for every scenario, and report them all. A turn that raises
is a failure of the scenario, not of the run.

A runner MUST also check, across the whole session and on every scenario, that
every receipt the session produced is named by at least one reply, and that no
reply calls a write verified while the receipt it names says otherwise.

## Required scenario categories

| Id | Category | What it must prove | Reference scenario |
| --- | --- | --- | --- |
| C1 | Greeting and scope | The capability list is derived from the catalogue | `01_greeting` |
| C2 | Governed write | Approval on the exact payload, verified read-back, receipt; the same request again replays the ledger without writing | `02_restart_staging` |
| C3 | Second approval | `require_second_approval` needs two approvals and states how many stand | `03_restart_production` |
| C4 | Missing input then reject | A question is asked, answered, then the write is rejected and nothing runs | `04_rollback_rejected` |
| C5 | Goal replacement | A new request replaces a pending goal; no write happens | `05_goal_replacement` |
| C6 | Policy denial | A write outside policy is denied, with the rule's reason | `06_silence_denied` |
| C7 | Verification failure | An injected failed read-back yields a failed receipt, no success claim, and a blocked retry | `07_verification_failure` |
| C8 | Invalid plan | An undeclared operation is an outcome, with no approval and no ledger entry | `10_invalid_plan` |
| C9 | Cancellation | A pending approval cancelled by a new goal executes nothing; a new request gets a new approval id | `11_approval_cancelled` |
| C10 | Session limit | A write beyond `max_per_session` is denied, counting failed receipts | `12_max_per_session` |
| C11 | Approval with nothing pending | A bare approval executes nothing and answers from scope | `13_approval_with_nothing_pending` |
| C12 | Composite | Members run in order; the write inside one still stops for approval | `14_composite_triage` |
| C13 | Off scope | A request outside the catalogue is declined in derived words | `08_off_scope` |
| C14 | Language | The reply is written in the language of the turn | `09_italian_reply` |
| C15 | Proposal lifecycle | A proposal cannot reach `approved` or `published` without a named actor | `15_learning_lifecycle` |
| C16 | Publication | A published skill validates, versions and routes without a restart | `15_learning_lifecycle` |
| C17 | Policy re-evaluation after approval | The state read before the write carries the operation under a heavier rule; the approval that stands no longer covers it and nothing runs | `16_denied_at_approval` |

C17 belongs to level 2: it proves `OSKA-GOV-7`, which level 2 requires.

## Checklist

Every requirement, the module that satisfies it in the reference
implementation, and the proof. Paths are relative to the repository root. Every
row carries a proof; a row that did not would be listed again, with the reason,
under an "Open items" section at the end of this document.

### Integration (L1)

| Id | Module | Proof |
| --- | --- | --- |
| OSKA-INT-1 | `src/opsdeck/contracts/skill.py` | `tests/unit/test_catalog.py` |
| OSKA-INT-2 | `src/opsdeck/connectors/mock.py`, `http.py`, `shell.py` | `tests/unit/test_connectors.py` |
| OSKA-INT-3 | `src/opsdeck/planner/planner.py` | `tests/unit/test_planner.py` |
| OSKA-INT-4 | `src/opsdeck/contracts/skill.py` | `tests/unit/test_catalog.py` |
| OSKA-INT-5 | `src/opsdeck/catalog/validator.py`, `src/opsdeck/ontology/graph.py` | `tests/unit/test_catalog.py`, `tests/unit/test_ontology.py` |
| OSKA-INT-6 | `src/opsdeck/connectors/base.py` | `tests/unit/test_connectors.py` |
| OSKA-INT-7 | `src/opsdeck/connectors/shell.py` | `tests/unit/test_connectors.py` |
| OSKA-INT-8 | `src/opsdeck/connectors/http.py` | `tests/unit/test_connectors.py` |
| OSKA-INT-9 | `src/opsdeck/connectors/base.py`, `src/opsdeck/planner/planner.py` | `tests/unit/test_connectors.py` |
| OSKA-INT-10 | `src/opsdeck/connectors/registry.py` | `tests/unit/test_connectors.py` |
| OSKA-INT-11 | `src/opsdeck/governance/ledger.py` | `tests/unit/test_governance.py` |
| OSKA-INT-12 | `src/opsdeck/connectors/mock.py` | `tests/unit/test_connectors.py` |

### Ontology (L2)

| Id | Module | Proof |
| --- | --- | --- |
| OSKA-ONT-1 | `src/opsdeck/contracts/skill.py`, `src/opsdeck/ontology/graph.py` | `tests/unit/test_catalog.py`, `tests/unit/test_ontology.py` |
| OSKA-ONT-2 | `src/opsdeck/contracts/skill.py`, `src/opsdeck/catalog/validator.py` | `tests/unit/test_catalog.py`, `tests/unit/test_ontology.py` |
| OSKA-ONT-3 | `src/opsdeck/catalog/validator.py` | `tests/unit/test_catalog.py` |
| OSKA-ONT-4 | `src/opsdeck/contracts/skill.py`, `src/opsdeck/catalog/validator.py` | `tests/unit/test_catalog.py`, `tests/unit/test_ontology.py` |
| OSKA-ONT-5 | `src/opsdeck/catalog/validator.py` | `tests/unit/test_catalog.py` |
| OSKA-ONT-6 | `src/opsdeck/ontology/queries.py` | `tests/unit/test_ontology.py` |
| OSKA-ONT-7 | `src/opsdeck/contracts/skill.py` | `tests/unit/test_catalog.py` |
| OSKA-ONT-8 | `src/opsdeck/ontology/queries.py` | `tests/unit/test_ontology.py`, `tests/unit/test_cli.py` |
| OSKA-ONT-9 | `src/opsdeck/catalog/registry.py` | `tests/unit/test_catalog.py` |
| OSKA-ONT-10 | `src/opsdeck/catalog/validator.py` | `tests/unit/test_catalog.py` |
| OSKA-ONT-11 | `src/opsdeck/catalog/validator.py` | `tests/unit/test_catalog.py` |
| OSKA-ONT-12 | `src/opsdeck/ontology/graph.py` | `tests/unit/test_ontology.py` |
| OSKA-ONT-13 | `src/opsdeck/ontology/export.py` | `tests/unit/test_ontology.py` |

### Skill (L3)

| Id | Module | Proof |
| --- | --- | --- |
| OSKA-SKL-1 | `src/opsdeck/contracts/skill.py` | `tests/unit/test_catalog.py` |
| OSKA-SKL-2 | `src/opsdeck/contracts/skill.py` | `tests/unit/test_catalog.py` |
| OSKA-SKL-3 | `src/opsdeck/catalog/validator.py` | `tests/unit/test_catalog.py` |
| OSKA-SKL-4 | `src/opsdeck/catalog/validator.py` | `tests/unit/test_catalog.py` |
| OSKA-SKL-5 | `src/opsdeck/catalog/loader.py`, `validator.py` | `tests/unit/test_catalog.py` |
| OSKA-SKL-6 | `src/opsdeck/catalog/registry.py` | `tests/unit/test_catalog.py` |
| OSKA-SKL-7 | `src/opsdeck/catalog/registry.py` | `tests/unit/test_catalog.py` |
| OSKA-SKL-8 | `src/opsdeck/catalog/validator.py` | `tests/unit/test_catalog.py` |
| OSKA-SKL-9 | `src/opsdeck/catalog/validator.py`, `src/opsdeck/engine/factory.py` | `tests/unit/test_engine.py`, `tests/unit/test_cli.py` |
| OSKA-SKL-10 | `src/opsdeck/catalog/registry.py` | `tests/unit/test_catalog.py` |
| OSKA-SKL-11 | `src/opsdeck/catalog/resolver.py` | `tests/unit/test_resolver.py`, `examples/ops/scenarios/01_greeting.json` |
| OSKA-SKL-12 | `src/opsdeck/catalog/resolver.py` | `tests/unit/test_resolver.py` |
| OSKA-SKL-13 | `src/opsdeck/catalog/loader.py`, `registry.py` | `tests/unit/test_catalog.py` |
| OSKA-SKL-14 | `src/opsdeck/governance/capabilities.py` | `tests/unit/test_governance.py` |

### Governance (L4)

| Id | Module | Proof |
| --- | --- | --- |
| OSKA-GOV-1 | `src/opsdeck/governance/capabilities.py` | `tests/unit/test_governance.py`, `examples/ops/scenarios/10_invalid_plan.json` |
| OSKA-GOV-2 | `src/opsdeck/governance/policy_engine.py` | `tests/unit/test_governance.py` |
| OSKA-GOV-3 | `src/opsdeck/governance/policy_engine.py` | `tests/unit/test_governance.py` |
| OSKA-GOV-4 | `src/opsdeck/governance/approval.py`, `src/opsdeck/engine/execution.py` | `examples/ops/scenarios/02_restart_staging.json` |
| OSKA-GOV-5 | `src/opsdeck/governance/approval.py` | `tests/unit/test_governance.py`, `tests/unit/test_engine.py` |
| OSKA-GOV-6 | `src/opsdeck/engine/engine.py` | `examples/ops/scenarios/03_restart_production.json` |
| OSKA-GOV-7 | `src/opsdeck/engine/engine.py`, `src/opsdeck/engine/execution.py` | `examples/ops/scenarios/16_denied_at_approval.json` |
| OSKA-GOV-8 | `src/opsdeck/governance/ledger.py`, `src/opsdeck/engine/execution.py` | `tests/unit/test_engine.py` |
| OSKA-GOV-9 | `src/opsdeck/governance/ledger.py` | `tests/unit/test_governance.py` |
| OSKA-GOV-10 | `src/opsdeck/governance/verification.py` | `tests/unit/test_governance.py`, `examples/ops/scenarios/07_verification_failure.json` |
| OSKA-GOV-11 | `src/opsdeck/governance/ledger.py`, `src/opsdeck/engine/execution.py` | `examples/ops/scenarios/02_restart_staging.json` |
| OSKA-GOV-12 | `src/opsdeck/governance/ledger.py` | `tests/unit/test_governance.py`, `examples/ops/scenarios/12_max_per_session.json` |
| OSKA-GOV-13 | `src/opsdeck/engine/reply.py`, `src/opsdeck/engine/outcomes.py` | `tests/scenarios/test_scenarios.py`, `tests/unit/test_engine.py` |
| OSKA-GOV-14 | `src/opsdeck/audit/events.py`, `src/opsdeck/engine/execution.py` | `tests/unit/test_audit.py` |

### Cognition (L5)

| Id | Module | Proof |
| --- | --- | --- |
| OSKA-COG-1 | `src/opsdeck/index/`, `src/opsdeck/router/`, `src/opsdeck/planner/` | `tests/unit/test_layering.py` |
| OSKA-COG-2 | `src/opsdeck/index/discovery.py` | `tests/unit/test_index.py` |
| OSKA-COG-3 | `src/opsdeck/index/discovery.py` | `tests/unit/test_index.py`, `examples/ops/scenarios/08_off_scope.json` |
| OSKA-COG-4 | `src/opsdeck/index/discovery.py`, `src/opsdeck/planner/planner.py` | `tests/unit/test_index.py`, `tests/unit/test_planner.py` |
| OSKA-COG-5 | `src/opsdeck/router/__init__.py` | `tests/unit/test_router.py` |
| OSKA-COG-6 | `src/opsdeck/router/fast_path.py` | `tests/unit/test_router.py` |
| OSKA-COG-7 | `src/opsdeck/planner/planner.py` | `tests/unit/test_planner.py` |
| OSKA-COG-8 | `src/opsdeck/planner/planner.py` | `tests/unit/test_planner.py`, `examples/ops/scenarios/10_invalid_plan.json` |
| OSKA-COG-9 | `src/opsdeck/planner/planner.py` | `tests/unit/test_planner.py`, `examples/ops/scenarios/04_rollback_rejected.json` |
| OSKA-COG-10 | `src/opsdeck/planner/planner.py`, `src/opsdeck/engine/reply.py` | `tests/unit/test_planner.py` |
| OSKA-COG-11 | `src/opsdeck/planner/scripted.py` | `tests/scenarios/test_scenarios.py` |

### Interaction (L6)

| Id | Module | Proof |
| --- | --- | --- |
| OSKA-IXN-1 | `src/opsdeck/engine/session_store.py` | `tests/unit/test_engine.py` |
| OSKA-IXN-2 | `src/opsdeck/engine/engine.py` | `tests/scenarios/test_scenarios.py` |
| OSKA-IXN-3 | `src/opsdeck/engine/engine.py` | `examples/ops/scenarios/13_approval_with_nothing_pending.json` |
| OSKA-IXN-4 | `src/opsdeck/engine/engine.py` | `examples/ops/scenarios/11_approval_cancelled.json` |
| OSKA-IXN-5 | `src/opsdeck/engine/engine.py` | `tests/unit/test_engine.py` |
| OSKA-IXN-6 | `src/opsdeck/engine/engine.py` | `examples/ops/scenarios/05_goal_replacement.json` |
| OSKA-IXN-7 | `src/opsdeck/engine/engine.py`, `src/opsdeck/engine/goals.py` | `examples/ops/scenarios/04_rollback_rejected.json` |
| OSKA-IXN-8 | `src/opsdeck/engine/engine.py`, `src/opsdeck/engine/execution.py` | `examples/ops/scenarios/02_restart_staging.json`, `examples/ops/scenarios/14_composite_triage.json` |
| OSKA-IXN-9 | `src/opsdeck/engine/engine.py` | `tests/unit/test_engine.py`, `examples/ops/scenarios/14_composite_triage.json` |
| OSKA-IXN-10 | `src/opsdeck/engine/execution.py` | `examples/ops/scenarios/02_restart_staging.json` |
| OSKA-IXN-11 | `src/opsdeck/engine/state.py`, `src/opsdeck/governance/ledger.py` | `tests/unit/test_engine.py` |
| OSKA-IXN-12 | `src/opsdeck/contracts/common.py`, `src/opsdeck/services.py`, `src/opsdeck/cli/commands_run.py`, `src/opsdeck/api/routes.py` | `tests/unit/test_cli.py`, `tests/unit/test_api.py` |
| OSKA-IXN-13 | `src/opsdeck/engine/engine.py`, `src/opsdeck/engine/reply.py` | `tests/unit/test_engine.py` |

### Evolution (L7)

| Id | Module | Proof |
| --- | --- | --- |
| OSKA-EVO-1 | `src/opsdeck/learning/proposals.py` | `tests/unit/test_learning.py` |
| OSKA-EVO-2 | `src/opsdeck/learning/quality.py` | `tests/unit/test_learning.py` |
| OSKA-EVO-3 | `src/opsdeck/learning/lifecycle.py` | `tests/unit/test_learning.py` |
| OSKA-EVO-4 | `src/opsdeck/learning/lifecycle.py` | `tests/unit/test_learning.py` |
| OSKA-EVO-5 | `src/opsdeck/learning/lifecycle.py` | `tests/unit/test_learning.py` |
| OSKA-EVO-6 | `src/opsdeck/learning/lifecycle.py` | `tests/unit/test_learning.py` |
| OSKA-EVO-7 | `src/opsdeck/learning/lifecycle.py` | `tests/unit/test_learning.py` |
| OSKA-EVO-8 | `src/opsdeck/catalog/registry.py`, `src/opsdeck/learning/lifecycle.py` | `tests/unit/test_learning.py` |
| OSKA-EVO-9 | `src/opsdeck/learning/lifecycle.py`, `src/opsdeck/catalog/validator.py` | `tests/unit/test_learning.py`, `examples/ops/scenarios/15_learning_lifecycle.json` |

### Cross-cutting

| Id | Module | Proof |
| --- | --- | --- |
| OSKA-XC-1 | `src/opsdeck/contracts/` | `tests/unit/test_layering.py` |
| OSKA-XC-2 | `src/opsdeck/contracts/common.py` | `tests/unit/test_layering.py` |
| OSKA-XC-3 | `src/opsdeck/config.py`, `configs/runtime.json` | `tests/unit/test_config.py` |
| OSKA-XC-4 | `src/opsdeck/config.py`, `src/opsdeck/cli/main.py` | `tests/unit/test_cli.py` |
| OSKA-XC-5 | `src/opsdeck/audit/events.py` | `tests/unit/test_audit.py` |
| OSKA-XC-6 | `src/opsdeck/audit/sink.py` | `tests/unit/test_audit.py` |
| OSKA-XC-7 | `src/opsdeck/governance/ledger.py` | `tests/unit/test_governance.py` |
| OSKA-XC-8 | `src/opsdeck/learning/lifecycle.py` | `tests/unit/test_learning.py` |
| OSKA-XC-9 | `src/opsdeck/planner/scripted.py` | `tests/scenarios/test_scenarios.py` |
| OSKA-XC-10 | `tests/scenarios/test_scenarios.py` | `tests/scenarios/test_scenarios.py` |

## Reporting conformance

A conformance report SHOULD state: the level claimed, the catalogue it was run
against, the scenario directory, the counts of scenarios passed and failed per
category, and the filled checklist with any requirement marked as not
applicable justified in one sentence. A requirement MUST NOT be marked not
applicable because the system chose not to implement the layer it belongs to;
in that case the level is simply not met.
