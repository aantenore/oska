# Why OSKA

A model that can call tools can change things. That is the point of giving it
tools, and it is also the problem. This document is the argument behind the
design: what goes wrong when a model acts directly on enterprise systems, what
OSKA does instead, what each choice costs, and when you should not use it.

## The problem

Operational knowledge rots. A procedure is written in a wiki, correct on the
day it is written, and then the API changes, the service is renamed, and the
runbook stays. What is automated sits in a script nobody dares run under
pressure; what is not is done by hand at three in the morning, from memory.

Handing the job to a prompt-first agent does not fix that. If the prompt
describes what the agent can do, the description and the system drift apart
the same way the wiki did, and the code has no way to tell a real operation
from an invented one. The risk is not that the model is wrong about a fact. It
is that it is wrong about a write, the write goes out anyway, and nobody can
say afterwards what was sent, who agreed to it, or whether it worked. Or the
write never arrived, and is reported as a refusal.

On the first bad day an operator asks three questions, and a prompt-first
system cannot answer any of them from its artifacts:

- What is this system allowed to do, exactly?
- What did it do, and how do we know it worked?
- Who said it could?

Capability is whatever the prompt claims today. Evidence is whatever the model
said in its last message. Authority is whoever wrote the prompt. None of these
survives an audit, and none survives a change of model.

## What the name says

OSKA stands for the Ontology-first, Skill-driven Agentic architecture.

**Ontology first.** What exists is declared before anything acts on it: the
entities, how each is identified, the states it can be in, the legal moves
between those states, and how entities relate. The state of an object is read
from the system of record through a declared read, never inferred from the
conversation.

**Skill driven.** What can be done, and how, lives in versioned skill files on
top of the ontology: integrations with the closed list of operations each
system offers, procedures that use them, composites that chain procedures. The
runtime is generic; the skills make it specific. Adding a procedure is adding
a file, not shipping code.

**Agentic, under governance.** A model reads the request, picks the procedure,
writes the plan and phrases the reply. Everything it produces is checked by
code before it has an effect, and every effect on the world crosses a
governance boundary that no configuration can lower.

## The design choices, and what each one costs

**Declared operations only.** The model may plan only operations an
integration declares, with an input schema, an output schema and an entity
binding. An undeclared operation is an invalid plan and is never executed. A
confused model produces a rejected plan and an honest reply, not an
unexpected call.
*Cost:* the system cannot improvise. If nobody declared it, it cannot be done,
even when the underlying API would allow it.

**Plan, then execute, instead of a loop.** A tool-calling loop lets the model
call, observe and call again until it decides it is done. It is more capable
in the open case, and it has no point at which the intended effect exists as a
complete object: nothing to freeze, nothing to hash, nothing to show a person
before the effect occurs. Approval in a loop is approval of the next call,
asked again and again, which people learn to click through. OSKA asks the
model for one typed plan, validates it whole, and executes it under
governance, so approval has a payload to attach to. The reasoning is in
[spec/07-cognition-layer.md](../spec/07-cognition-layer.md#rationale).
*Cost:* less open-ended. Multi-step work is a declared composite or a goal the
runtime advances, not something the model discovers as it goes.

**A fixed write path.** Every write is checked against the declarations,
looked up in the ledger, judged by a policy, checked as a legal move from the
state the object is in, frozen on its exact payload and held for approval,
recorded as an intent, executed, read back, and only then reported. A missing
policy denies. No setting turns a step off, because the path that performs
these steps is the only path from a plan to a connector. See
[governance.md](governance.md#the-write-path).
*Cost:* a person in the loop for every write, and a second one when the policy
asks for it. That is latency, and it is the point.

**Read-back verification.** A write declares the read that proves it worked,
and the expected values. The reply reports what the read-back confirmed, and
a write that was not verified is reported as not verified. The deterministic
line for every receipt is appended to whatever the model wrote, so the account
of a write comes from the receipt even when the prose does not.
*Cost:* every write needs a read that can prove it. If you cannot name one,
that is worth knowing before anything automates the write.

**An idempotent ledger.** A write's identity is a hash of the integration, the
operation and the payload fields it declares as its idempotency key. Asking for the same thing again in
the same session returns the original receipt instead of writing twice.
*Cost:* the runtime keeps receipts, and the author of an integration has to
say which fields make two requests the same request.

**A capability that is down is not a refusal.** A connector says whether the
system answered no, whether the request was never well formed, or whether it
never arrived; and among those that never arrived, whether nothing was sent or
the outcome is unknown. Only the last two open a circuit breaker, and neither
settles anything in the ledger. See
[governance.md](governance.md#when-the-system-does-not-answer).

**A voice derived from the catalog.** What the system says it can do, in the
greeting, the refusal and the prompt that classifies a request, is computed
from the published catalog. Retiring a procedure removes it from what the
system offers in the same deploy, without editing any text.
*Cost:* the wording of capabilities comes from declarations. A team that wants
different words changes the declaration, not a prompt.

**Promises are declared, not asserted.** Every promise the system makes about
its own behavior sits in a guardrail catalog with the point it acts at and the
mechanism that keeps it: a check the code runs, an invariant a named module
keeps, or text handed to a model. A reviewer can see, line by line, which
promises survive a model that ignores them.

**A turn costs what it costs.** A feature does not add a model call to a turn.
Work that needs a model, such as deriving routing examples or paraphrases in
other languages, runs at publish time or in a scheduled job, and a turn spends
no more calls than its configured budget.
*Cost:* some things are computed ahead of time and can be stale until the next
publish; a stale derived file is ignored rather than trusted.

**Human-controlled evolution.** A system can draft a procedure from a session
it just ran, and it cannot grant itself a capability. A proposal moves state
by state with a named actor on every move; publishing one composes a change
request for a person to review. The self-improvement job declares how far it
may go without a person, as a ladder whose top rung, publishing a capability,
has no code path. See [improvement.md](improvement.md) and
[learning.md](learning.md).
*Cost:* learning is slower than letting the system edit itself. That is the
trade.

**Isolation is structural.** A platform serves several domains from one
process. A domain's catalog, connectors, guardrails and state are its own
packs' and nothing else's; a tenant changes wording, languages and resilience,
never identity, storage or the catalog. See
[packs-and-platforms.md](packs-and-platforms.md#isolation).

**Replaceable cognition.** The model, the index, the router and the reply
writer are providers behind interfaces. The whole conformance suite runs
offline against scripted answers or a recorded session, so what passes is the
architecture, not a well-behaved model. A change of model is a configuration
change, and the scenario suite proves the behavior that survives it.

## What it adds up to

An OSKA system is explainable, because every action traces to a declared
operation in a readable, versioned file. It is auditable, because every step
of every turn is a typed event and every write is a receipt with its read-back
attached. It is safe by construction rather than by instruction, because the
guarantees live in the order the code runs, not in a sentence the model is
asked to respect.

It owns no business data. Deployed on top of existing systems, it reads and
writes through the integration layer and keeps only conversation state,
receipts and audit. Removing it loses the trail of what it did, and nothing
else.

The trade is explicit. OSKA gives up open-ended autonomy. It cannot invent a
capability at run time, and that is not a limitation to be removed later; it
is the property that makes the rest hold.

## When not to use OSKA

- **Open-ended work.** Research, coding, browsing, data exploration: tasks
  whose steps are not known in advance and whose value comes from the model
  finding them. A tool-calling loop fits those better.
- **Nothing to govern.** Answering questions over documents, summarizing,
  drafting text a person will edit anyway. If no write reaches a system of
  record, most of what OSKA enforces has nothing to act on.
- **Operations you cannot name.** If you cannot list the operations, their
  schemas and the entity each acts on, the catalog cannot be written, and that
  is the first thing to fix.
- **Writes you cannot verify.** If no read can prove a write worked, OSKA will
  make you say so for every one of them.
- **No person available to approve.** High-frequency, fully unattended writes
  are not what the write path is for. Every write waits for a person.
- **A fixed script is enough.** If the request is always the same and the
  steps never change, a script or a workflow engine is simpler and cheaper.

## Read next

- [spec/00-overview.md](../spec/00-overview.md): the thesis and the non-goals, as the standard states them.
- [spec/01-principles.md](../spec/01-principles.md): the ten principles, each with the consequence that makes it testable.
- [spec/06-governance-layer.md](../spec/06-governance-layer.md): the write path as requirements.
- [spec/07-cognition-layer.md](../spec/07-cognition-layer.md#rationale): why a plan and not a loop.
- [building-a-platform.md](building-a-platform.md): try it, from nothing to a checked platform.
