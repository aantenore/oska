# 07 Cognition layer (L5)

## Purpose

L5 is where a language model is allowed to act, and the only place. It does
four things: it ranks which declared procedure a request is about, it
classifies what the user is doing in this turn, it produces a typed plan inside
one procedure's declarations, and it phrases a reply from outcomes that already
happened.

It executes nothing. It emits a plan; L6 runs it through L4. This is the
dependency rule's sharpest edge, and it is what gives approval somewhere to
stand.

## Modules

### Discovery

Responsibility: given a request in the user's words, rank the published
procedures and choose one, or choose none.

```text
SkillIndex:
  build(skills) -> void
  rank(query, limit) -> [RankedSkill]     # {skill_id, score 0..1, matched_on}

Discovery:
  select(query) -> DiscoveryResult        # {ranked, chosen, tie}
```

The indexed text of a procedure is its description and each of its triggers,
indexed separately; a procedure's score is the best of its texts. Two index
implementations are expected: a lexical one that needs no model provider, and
an embedding one that uses `embed`. Composites are excluded from ranking by
configuration, because a composite's triggers overlap its members' by
construction and it would always win or always lose for the wrong reason.

A tie — the top two within a configured margin, both above a minimum score — is
resolved by asking the model to pick between the ranked candidates. With no
provider, the top candidate wins. With nothing above the minimum score, nothing
is chosen and the turn is out of scope.

### Router

Responsibility: decide what kind of turn this is.

```text
TurnKind: new_task | continue_task | provide_input | approve | reject
        | greeting | off_scope | cancel_task

RouteDecision:
  kind: TurnKind
  procedure_id: string | null
  confidence: number
  source: rule | fast_path | model
  language: string | null
```

Three stages, in order, first answer wins:

1. **Rules.** Deterministic, driven by configured token lists and by what the
   session is waiting for. An approval token while an approval is pending is an
   approval. A rejection token is a rejection. A cancellation token cancels.
2. **Fast path.** A lexical match against the triggers of published procedures
   and against configured greeting utterances, above a configured threshold,
   and only when nothing is pending. Any failure falls through.
3. **Model.** A classification call whose prompt carries the declared scope from
   L3. It may also report the language of the turn.

When the user asks for a different outcome while a goal is pending, the router
returns `new_task`. The engine replaces the pending goal. It never merges two
goals.

### Planner

Responsibility: produce a typed plan for one hydrated procedure.

```text
PlannedOperation: {integration_id, operation, payload, write, purpose}
Plan: {procedure_id, operations, missing: [{field, question}], reply_intent}

Planner:
  plan(hydrated, conversation, collected_inputs) -> Plan
  pick(query, candidates) -> procedure_id
  write_reply(outcomes, reply_intent, language, suggestions) -> string
```

The prompt is built from the hydrated closure: the procedure's sections,
the entity fields, and the exact declared operations with their input schemas.
The plan is then validated: every operation must be declared by one of the
listed integrations, every write must be listed in the procedure's
`capabilities.writes`, and every payload must satisfy the operation's input
schema. An invalid plan is repaired once, by handing the issues back. A plan
that is still invalid is an outcome — `invalid_plan` — not an exception, and
the user is told the request could not be carried out, not shown a stack trace.

Inputs the plan cannot fill are returned as `missing`, each with the question to
ask. Asking is a first-class result, not a failure.

### Reply Writer

Responsibility: phrase what happened, in the user's language, without adding
anything.

It receives the outcome list, the reply intent from the plan, and the
suggestions the resolver derived from the entities the reads touched. It may
state only what is in the outcome list, and may offer only the suggestions it
was given. A deterministic template renderer produces the same reply when no
model provider is configured.

What the turn wrote is not left to the phrasing. Whatever a model returns, the
deterministic line of every receipt and every ledger hit of the turn is
appended to it, so the account of a write always comes from the receipt and
carries its identifier. A reply that omitted the write, or described it more
kindly than the receipt allows, is answered by the lines that follow it.

### Provider interface

Responsibility: make cognition replaceable.

```text
ModelProvider:
  name: string
  complete_json(system, messages, schema, temperature) -> object
  embed(texts) -> [[number]]
```

Two implementations are expected: one over a real service, and a **scripted**
one that returns pre-recorded responses in order, matching on a substring of
the prompt it was given and failing loudly when the script runs out. The
scripted provider is what makes the conformance suite deterministic and
offline.

## Invariants

- The engine, not the model, chooses which procedure runs: the model ranks and
  classifies, the engine decides and records the decision.
- A plan is validated against declarations before anything runs.
- The model may emit only operations in the hydrated closure it was given.
- Cognition never calls a connector, a ledger or a policy engine.
- A new goal replaces a pending goal; goals are never merged.
- The reply writer may state only outcomes that occurred and suggestions it was
  handed.
- Every model call is replaceable by a scripted response without changing any
  other layer.
- A missing provider degrades the system to catalogue-derived answers; it never
  degrades governance.

## Requirements

- **OSKA-COG-1** — The cognition layer MUST NOT call a connector, the ledger,
  the policy engine or the approval gate.
- **OSKA-COG-2** — Discovery MUST rank only published procedures, and MUST
  exclude composites from ranking.
- **OSKA-COG-3** — Discovery MUST return no choice when no candidate reaches the
  configured minimum score, and the turn MUST then be treated as out of scope.
- **OSKA-COG-4** — A tie within the configured margin MUST be resolved by a
  model pick restricted to the ranked candidates, or by the top candidate when
  no provider is configured.
- **OSKA-COG-5** — Routing MUST evaluate rules first, then the fast path, then
  the model, and MUST record which stage decided.
- **OSKA-COG-6** — The fast path MUST be skipped when the session has a pending
  goal or a pending approval.
- **OSKA-COG-7** — A plan MUST be rejected if it names an operation not declared
  in the hydrated closure, a write not listed in the procedure's capabilities,
  or a payload that fails the operation's input schema.
- **OSKA-COG-8** — An invalid plan MUST be offered for repair at most once, and
  a plan that is still invalid MUST produce an `invalid_plan` outcome with no
  execution and no approval request.
- **OSKA-COG-9** — Inputs the plan cannot supply MUST be returned as `missing`
  entries with a question each, and MUST NOT be invented.
- **OSKA-COG-10** — The reply writer MUST state only outcomes present in the
  outcome list and MUST offer only suggestions derived from the published
  catalogue.
- **OSKA-COG-11** — A conformant system MUST provide a scripted provider that
  satisfies the provider interface, and MUST be able to run its entire
  conformance suite with it, offline.

## Rationale

The obvious alternative is the agent loop: give the model the tools, let it
call them, observe, and repeat until it decides it is done. It is more capable
in the open case and it is the wrong shape here. The loop has no point at which
the intended effect exists as a complete object, so there is nothing to freeze,
nothing to hash, and nothing to show a human before the effect occurs. Approval
in a loop is approval of the next call, asked repeatedly, which people learn to
click through. A plan produced in one step, validated in one step and executed
under governance gives approval a payload to attach to.

Splitting routing into rules, fast path and model is not an optimisation, or
not only one. The rules stage exists because some classifications must not
depend on a model at all: whether the word "yes" approves a pending write is a
question about session state, and a model that gets it wrong approves something
nobody approved. The fast path exists because most turns are one of a handful
of known requests, and a lexical match against declared triggers is both
cheaper and more predictable than a classification call. The model stage
handles what is left, which is the interesting part.

Constraining the reply writer to stated outcomes is the least glamorous rule
here and the one that changes the system's character most. A model asked to
summarise what happened will smooth over a failed read-back, because fluent
prose prefers a clean ending. Handing it a typed outcome list, and forbidding
anything outside it, is what makes "the restart is unverified" a sentence the
system can actually produce. The conformance suite checks this directly: no
reply may call a write verified unless its receipt says so.

## In the reference implementation

- `src/opsdeck/index/` — `base.py`, `keyword.py`, `embedding.py`,
  `discovery.py`.
- `src/opsdeck/router/` — `base.py` (rules), `fast_path.py`,
  `model_router.py`.
- `src/opsdeck/planner/` — `provider.py` (the interface), `scripted.py`, the
  service-backed provider implementation, `planner.py`, `prompts.py`.
- `src/opsdeck/engine/reply.py` — the deterministic renderer used when no
  provider is configured.
