# OSKA

OSKA 0.1, draft. Reference implementation: the `oska` package in this repository.

OSKA is the Ontology-first, Skill-driven Agentic architecture: a layered
architectural standard for systems in which a language model acts on real
systems on behalf of a person. It names layers, modules, contracts and
invariants. It names no vendor, no model, no framework and no database.

An OSKA system declares what exists before it declares what can be done, and
declares what can be done before it lets a model decide anything. Every effect
on the world crosses a governance boundary that no configuration can lower.
What the system says it can do is computed from its catalog, not written by
hand into a prompt.

## Status

Draft. Requirement identifiers are stable within 0.x only by intent, not by
promise. Normative keywords (MUST, MUST NOT, SHOULD, SHOULD NOT, MAY) are used
as defined in RFC 2119. A requirement written without a normative keyword is
descriptive, not binding.

## How to read

Read in order on a first pass:

| Document | What it covers |
| --- | --- |
| [00-overview.md](00-overview.md) | The problem, the thesis, scope and non-goals |
| [01-principles.md](01-principles.md) | The eight principles the layers implement |
| [02-layers.md](02-layers.md) | The layered model and the dependency rule |
| [03-integration-layer.md](03-integration-layer.md) | L1: declared operations and connectors |
| [04-ontology-layer.md](04-ontology-layer.md) | L2: entities, relations, bindings, vocabulary |
| [05-skill-layer.md](05-skill-layer.md) | L3: the skill contract, catalog, resolver |
| [06-governance-layer.md](06-governance-layer.md) | L4: the write path |
| [07-cognition-layer.md](07-cognition-layer.md) | L5: discovery, routing, planning, replying |
| [08-interaction-layer.md](08-interaction-layer.md) | L6: session, turn algorithm, channels |
| [09-evolution-layer.md](09-evolution-layer.md) | L7: proposals, gates, lifecycle, publication |
| [10-cross-cutting.md](10-cross-cutting.md) | Configuration, observability, identity, evaluation |
| [11-deployment-topologies.md](11-deployment-topologies.md) | Overlay, standalone, multi-agent, trust boundaries |
| [12-conformance.md](12-conformance.md) | Levels, scenario format, requirement checklist |
| [13-glossary.md](13-glossary.md) | Terms, with the one meaning each carries here |
| [reference-implementation.md](reference-implementation.md) | Layer-to-package map |
| [schemas/README.md](schemas/README.md) | The normative machine-readable schemas |

Implementers who already know the model can go straight to
[12-conformance.md](12-conformance.md): it lists every requirement, the module
that satisfies it in the reference implementation, and the test that proves it,
with the requirements nothing yet proves gathered under "Open items".

Each layer document has the same shape: purpose, modules, invariants, a
numbered requirement list, a rationale, and a pointer into the reference
implementation. Requirement identifiers are `OSKA-<LAYER>-<n>`, where `<LAYER>`
is `INT`, `ONT`, `SKL`, `GOV`, `COG`, `IXN`, `EVO` or `XC`.

## Conformance in one paragraph

A system conforms at Level 1 (Declared) when its catalog validates, its
capability copy is derived and it runs offline against a scripted model
provider. At Level 2 (Governed) it also satisfies the whole write path and
passes the governed scenario categories. At Level 3 (Adaptive) it also
satisfies the evolution requirements. Conformance is demonstrated by running a
scenario suite, not by inspection.

## Reference implementation

The `oska` package in this repository is the reference implementation, and there are two reference
conformance domains over two catalogs that share no vocabulary. The reference
implementation is normative only where this specification points at it; where
the two disagree, this specification wins.

There are two reference suites, and a level is attained per suite. Run against
`examples/ops/scenarios`, the operational suite attains Level 3 (Adaptive).
Run against `examples/access/scenarios`, the access suite attains Level 2
(Governed): it declares no scenario of level 3, so it claims none. Every
scenario of both suites passes, as `oska conform` reports for each.

## How to cite

```
Antonio Antenore. OSKA: Ontology-first, Skill-driven Agentic architecture,
version 0.1 (draft). 2026.
```

## License

The specification text follows the license of the repository it ships in.
