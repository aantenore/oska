# Documentation

Two sets of documents live here. `docs/` says how this implementation works
and how to use it. [`spec/`](../spec/README.md) is the standard it implements;
where the two disagree, the standard wins. Start from the group that matches
what you came to do.

## Evaluating it

- [../README.md](../README.md): what OSKA is, a replayed session, and the quickstart.
- [why-oska.md](why-oska.md): the problem, the design choices and their cost, and when not to use it.
- [../spec/00-overview.md](../spec/00-overview.md): the thesis, the scope and the non-goals of the standard.
- [../spec/01-principles.md](../spec/01-principles.md): the ten principles every layer implements.
- [architecture.md](architecture.md): the runtime package by package, and what one turn does.

## Building a platform

- [building-a-platform.md](building-a-platform.md): from `oska new pack` to a signed, token-authenticated platform on PostgreSQL with CI and metrics.
- [adopting.md](adopting.md): declaring an existing system by hand, one declaration at a time.
- [packs-and-platforms.md](packs-and-platforms.md): the pack, the platform manifest, domains, tenants and the isolation between them.
- [configuration.md](configuration.md): every environment variable and every key of `runtime.json` and `model.json`.
- [multilingual.md](multilingual.md): reaching a catalog in more languages than it was written in, and the runtime's own sentences per language.
- [presentation.md](presentation.md): presentation directives and subject tracking, for an interface that renders more than text.

## Writing packs

- [skill-contract.md](skill-contract.md): the skill file format for entities, integrations, procedures and composites, and the validator rules.
- [../examples/ops/README.md](../examples/ops/README.md): the reference pack, services, deployments, incidents and alerts.
- [../examples/access/README.md](../examples/access/README.md): the second reference pack, the smallest one that exercises the whole write path.
- [conformance.md](conformance.md): the scenario format, the levels, recording a session against a real model and replaying it.
- [learning.md](learning.md): drafting a procedure from a session, its lifecycle, and the change request publication composes.

## Operating an installation

- [operating.md](operating.md): releasing a catalog, deciding on a change request, the service-level report, forgetting a conversation, a connector that is down.
- [governance.md](governance.md): the write path, lifecycles, failure classification, guardrails, policy, approval, verification, the ledger, audit, identity and retention.
- [improvement.md](improvement.md): the self-improvement job, the autonomy ladder and how to revert what it did.
- [../ops/slo.md](../ops/slo.md) and [../ops/alerts.yml](../ops/alerts.yml): the service-level targets and the alerting rules that match them.

## The standard

| Document | What it covers |
| --- | --- |
| [spec/README.md](../spec/README.md) | How to read the standard, conformance in one paragraph, how to cite it |
| [00-overview.md](../spec/00-overview.md) | The problem, the thesis, scope and non-goals |
| [01-principles.md](../spec/01-principles.md) | The ten principles the layers implement |
| [02-layers.md](../spec/02-layers.md) | The layered model and the dependency rule |
| [03-integration-layer.md](../spec/03-integration-layer.md) | L1: declared operations and connectors |
| [04-ontology-layer.md](../spec/04-ontology-layer.md) | L2: entities, relations, bindings, vocabulary |
| [05-skill-layer.md](../spec/05-skill-layer.md) | L3: the skill contract, catalog, resolver |
| [06-governance-layer.md](../spec/06-governance-layer.md) | L4: the write path |
| [07-cognition-layer.md](../spec/07-cognition-layer.md) | L5: discovery, routing, planning, replying |
| [08-interaction-layer.md](../spec/08-interaction-layer.md) | L6: session, turn algorithm, channels |
| [09-evolution-layer.md](../spec/09-evolution-layer.md) | L7: proposals, gates, lifecycle, publication |
| [10-cross-cutting.md](../spec/10-cross-cutting.md) | Configuration, observability, identity, evaluation |
| [11-deployment-topologies.md](../spec/11-deployment-topologies.md) | Overlay, standalone, multi-agent, trust boundaries |
| [12-conformance.md](../spec/12-conformance.md) | Levels, scenario format, the requirement checklist and what proves each item |
| [13-glossary.md](../spec/13-glossary.md) | Terms, with the one meaning each carries here |
| [14-platform-blueprint.md](../spec/14-platform-blueprint.md) | Packs, manifests, domains and tenants |
| [reference-implementation.md](../spec/reference-implementation.md) | Which package implements which layer |
| [schemas/README.md](../spec/schemas/README.md) | The normative machine-readable schemas |

## About the project

- [../CHANGELOG.md](../CHANGELOG.md): what changed, release by release.
- [../CONTRIBUTING.md](../CONTRIBUTING.md): setup and the checks a change runs.
- [../SECURITY.md](../SECURITY.md): the security model and how to report a vulnerability.
- [../LICENSE](../LICENSE): MIT.
