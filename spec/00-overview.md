# 00 Overview

## The problem

Agent systems are usually built prompt-first. The prompt describes what the
agent can do. The code hopes the model behaves. Capability drifts away from the
prompt as soon as a tool changes. Nobody can answer, from the artefacts alone,
three questions an operator will ask on the first bad day:

- What is this system allowed to do, exactly?
- What did it do, and how do we know it worked?
- Who said it could?

The usual answers are unsatisfying. Capability is whatever the prompt currently
claims. Evidence is whatever the model said in its last message. Authority is
whoever wrote the prompt. None of these survive an audit, and none of them
survive a change of model.

## The thesis

OSKA inverts the order. The ontology comes first: what exists, how it is
identified, what states it can be in, and which moves between those states are
legal. The state of an object is read from the system of record through a
declared read, never inferred from the conversation. Skills are declared on top
of it: what can be done, and how. The model reasons only inside what is
declared. Every effect on the world passes through governance. The catalogue,
not the prompt, is the source of truth for what the agent says it can do. The
catalogue evolves only through a human-controlled lifecycle.

A system built this way is:

- **Explainable.** Every action traces to a declared operation in a versioned
  skill file, and every skill file is readable by the people who own the
  procedure.
- **Auditable.** Every step of every turn is a typed event. Every write is a
  receipt with a read-back result attached.
- **Safe by construction.** A write cannot bypass approval, verification or the
  ledger, because the path that executes writes is the only path that reaches a
  connector, and it performs those steps in a fixed order.
- **Honest about what holds it.** Every promise the system makes about its own
  behaviour is declared once, with the point at which it acts and whether it is
  enforced by code at that point or handed to the model as text. The catalogue
  of promises says which; a sentence in a prompt is never presented as a
  control.
- **Portable.** The model, the retrieval index, the router and the reply writer
  are providers behind interfaces. Replacing one does not change the
  catalogue, the governance or the audit trail.

The trade is explicit. OSKA gives up open-ended autonomy. An OSKA system cannot
invent a new capability at run time, and this is not a limitation to be removed
later; it is the property that makes the rest of it hold. A system that learns
from its own traffic says how far it may go without a person, as a ladder whose
rungs are named and whose top rung — publishing a capability — is never
reached automatically.

## Scope

This specification covers:

- The eight-layer model, its modules and the dependency rule between them.
- The skill contract: the declarative artefact that carries entities,
  integrations, procedures and composites.
- The write path: declaration, policy, approval, execution, verification,
  ledger, audit.
- The turn algorithm: how one user turn becomes routing, planning, execution
  and a reply.
- The evolution lifecycle: how a new procedure enters the catalogue, and the
  autonomy ladder a system that learns from its own traffic has to declare
  itself on.
- Conformance: levels, the scenario format, and the checklist that maps every
  requirement to its proof.

## Non-goals

This specification does not cover:

- **Systems of record.** The world an OSKA system acts on is out of scope. It
  is reachable only through the integration layer.
- **Model quality.** Nothing here makes a weak model good. The guarantees are
  structural: a bad plan is rejected, a bad reply is constrained to verified
  facts, a bad write is blocked before it runs.
- **Authentication and authorisation of humans.** OSKA assumes an
  authenticated channel and records the actor it is given. Establishing who
  that actor is belongs to the deployment.
- **Prompt engineering.** Prompt text is an implementation detail of the
  cognition layer. The specification constrains what a prompt is built from,
  not how it is worded.
- **A wire protocol.** OSKA is an architecture, not an API. The reference
  implementation exposes one; another conformant system may expose a different
  one.
- **Transport, storage and scheduling technology.** Any store that satisfies
  the stated invariants is acceptable.

## What a conformant system looks like from outside

A person asks for something in their own words. The system decides which
declared procedure that is, or says it is out of scope in words derived from
its own catalogue. It reads what it needs. If the procedure writes, it stops
and shows the exact payload it intends to send, and it waits. On approval, it
records the intent, sends the write, reads the result back, and reports only
what the read-back confirmed. If the read-back disagrees, it says so and does
not retry. Asking for the same thing again in the same session returns the
original receipt instead of writing twice.

Everything in that paragraph is a requirement somewhere in this document set.

## Terminology

Terms are defined once, in [13-glossary.md](13-glossary.md). Where a word has a
common loose meaning and a narrow meaning here (skill, capability, plan,
receipt, verification), the narrow meaning is the one this specification uses.

Normative keywords follow RFC 2119. "Model provider" means any component that
answers a structured completion or embedding request; no vendor is named in
this specification.
