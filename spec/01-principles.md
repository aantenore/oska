# 01 Principles

Eight principles. Every layer implements at least one; no layer contradicts
any. Each principle is stated, then the consequence that makes it testable.

## 1. Ontology first

Entities, identifiers, states and relations are declared before any procedure
is written. Every operation is bound to an entity type. Every skill names the
entities it touches.

Consequence: a procedure that mentions a record the ontology does not declare
fails validation. There is no implicit entity, and no entity that exists only
inside a prompt.

## 2. Skill-driven

Behaviour lives in versioned, declarative skill artefacts, not in code and not
in prompts. The runtime is generic. The skills make it specific.

Consequence: the same binary serves an entirely different domain by pointing at
a different catalogue root. Adding a procedure is adding a file, not shipping
code.

## 3. Declared operations only

The model may plan only operations that an integration skill declares, with a
schema. An undeclared operation is an invalid plan and is never executed.

Consequence: the failure mode of a confused model is a rejected plan and an
honest reply, not an unexpected call. The blast radius of the model is bounded
by a list a human wrote.

## 4. Governed effects

Every write traverses, in this order: capability check, policy, exact-payload
approval, recorded intent, execution, read-back verification, ledger update,
audit. No configuration can disable a step. A missing policy denies.

Consequence: there is exactly one code path from a plan to a connector for a
write, and it is the path that performs those steps. Safety is not a flag.

## 5. Catalogue-derived voice

What the system says it can do is computed from the published catalogue. There
is no hand-written capability copy, in the greeting, in the refusal, or in the
prompt that asks the model to classify a request.

Consequence: retiring a procedure removes it from what the system offers, in
the same deploy, without editing any text. Capability drift is structurally
impossible.

## 6. Replaceable cognition

The model, the embedding index, the router and the reply writer are providers
behind interfaces. A conformant system MUST be able to run its whole
conformance suite offline against a scripted provider.

Consequence: tests are deterministic and cost nothing. A change of model
provider is a configuration change, and the scenario suite proves the
behaviour that survives it.

## 7. Human-controlled evolution

New or changed skills enter through proposals with quality gates and explicit
human transitions. There is no code path that publishes without a named actor.

Consequence: a system can learn a procedure from a session it just ran, and
still cannot grant itself a new capability. Learning produces a candidate; a
person produces a capability.

## 8. Overlay, not ownership

When deployed on top of enterprise systems, the agent owns no business data. It
reads and writes through the integration layer and keeps only conversation
state, receipts and audit.

Consequence: the agent is removable. Deleting it loses the trail of what it
did, and loses nothing else. No system of record is behind it.

## How the principles constrain each other

Principles 1 and 3 together mean the model's action space is the transitive
closure of declarations, and nothing else. Principle 5 then makes the system's
description of itself a function of the same declarations, so what it claims
and what it can do cannot diverge.

Principles 4 and 7 are the two places where a human is required. Everything
else may be automated. This is deliberate: the two irreversible acts in an
agent system are performing a write and granting a new capability, and both are
gated by a named person.

Principle 6 is what keeps the rest honest. If the guarantees hold only when a
particular model is well behaved, they are not guarantees. Running the suite
against a scripted provider removes the model from the argument: what passes is
the architecture.
