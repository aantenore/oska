# 01 Principles

Ten principles. Every layer implements at least one; no layer contradicts any.
Each principle is stated, then the consequence that makes it testable.

## 1. Ontology first

Entities, identifiers, states and relations are declared before any procedure
is written. Every operation is bound to an entity type. Every skill names the
entities it touches.

Consequence: a procedure that mentions a record the ontology does not declare
fails validation. There is no implicit entity, and no entity that exists only
inside a prompt.

The state an object is in is part of the declaration, and it is read from the
system of record through a declared read. It is never inferred from the
conversation, from an earlier turn, or from what a write replied.

## 2. Skill-driven

Behavior lives in versioned, declarative skill artifacts, not in code and not
in prompts. The runtime is generic. The skills make it specific.

Consequence: the same binary serves an entirely different domain by pointing at
a different catalog root. Adding a procedure is adding a file, not shipping
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

## 5. Catalog-derived voice

What the system says it can do is computed from the published catalog. There
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
behavior that survives it.

## 7. Human-controlled evolution

New or changed skills enter through proposals with quality gates and explicit
human transitions. There is no code path that publishes without a named actor.

Consequence: a system can learn a procedure from a session it just ran, and
still cannot grant itself a new capability. Learning produces a candidate; a
person produces a capability.

A system that learns from its own traffic declares how far it may go without a
person, as a ladder: the rung it stands on is named, and every rung above it is
unreachable by a code path rather than by intent. The top rung — publishing a
capability — is never automatic.

## 8. Overlay, not ownership

When deployed on top of enterprise systems, the agent owns no business data. It
reads and writes through the integration layer and keeps only conversation
state, receipts and audit.

Consequence: the agent is removable. Deleting it loses the trail of what it
did, and loses nothing else. No system of record is behind it.

## 9. A turn costs what it costs

A feature does not add a model call to a turn. Work that needs a model is
performed at publish time or by a scheduled job, and a turn spends no more
model calls than the configured budget allows.

Consequence: the cost and the latency of a turn are bounded by configuration
rather than by how many features are switched on, and a turn that reaches its
budget answers from what it already holds instead of failing.

## 10. Promises are declared, not asserted

Every promise the system makes about its own behavior is declared once, in a
catalog of guardrails, with the point it acts at — input, planning, execution
or output — and the mechanism that keeps it. A promise the guardrail chain
enforces names the check that enforces it and carries every value that check
reads; a promise kept elsewhere names the module that keeps it and claims no
check and no values, because a value nothing reads is a promise nothing keeps;
a promise handed to the model is text. The declaration says which, rather than
presenting any of them as something it is not.

Consequence: a reviewer can read the list of promises and see, per line, which
ones survive a model that ignores them. A sentence in a prompt cannot be
counted as a safeguard, because the artifact that would have to count it says
what it is.

## How the principles constrain each other

Principles 1 and 3 together mean the model's action space is the transitive
closure of declarations, and nothing else. Principle 5 then makes the system's
description of itself a function of the same declarations, so what it claims
and what it can do cannot diverge.

Principle 10 sharpens principle 4 rather than adding to it: the write path is
the enforced promise, and every other promise has to declare whether it is of
that kind or of the other. Principle 9 constrains principles 5 and 6 in the
same way, by making derived text a publish-time artifact rather than a turn-time
one.

Principles 4 and 7 are the two places where a human is required. Everything
else may be automated. This is deliberate: the two irreversible acts in an
agent system are performing a write and granting a new capability, and both are
gated by a named person.

Principle 6 is what keeps the rest honest. If the guarantees hold only when a
particular model is well behaved, they are not guarantees. Running the suite
against a scripted provider removes the model from the argument: what passes is
the architecture.
