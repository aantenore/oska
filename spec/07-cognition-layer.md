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
indexed separately, and, when they exist, the phrases derived from those
triggers at publish time and the phrasings a scheduled job learned from traffic
that reached the procedure; a procedure's score is the best of its texts. Two index
implementations are expected: a lexical one that needs no model provider, and
an embedding one that uses `embed`. Composites are excluded from ranking by
configuration, because a composite's triggers overlap its members' by
construction and it would always win or always lose for the wrong reason.

A tie — the top two within a configured margin, both above a minimum score — is
resolved by asking the model to pick between the ranked candidates. With no
provider, the top candidate wins. With nothing above the minimum score, nothing
is chosen and the turn is out of scope.

### Reaching a catalog in more languages than it was written in

Responsibility: let a catalog whose triggers were written in one language be
reached by requests in the languages it is actually used in, without a second
set of authored triggers and without spending a model call on a turn.

An author writes the trigger examples of a procedure once, in the language they
work in. Translating each of them by hand does not scale, and a hand
translation rots the moment a trigger is edited: nothing relates the two, so the
rot is silent. Translating the request, or the catalog, while a turn is in
flight moves the cost to the worst possible place — every turn pays it, and both
the latency and the failure modes of a turn grow with the number of languages
supported.

So the paraphrases are derived once, when the procedure is published or when a
scheduled job asks for them, and are stored beside the skill:

```text
enrich(skill, provider, config, languages) -> {language: [phrase]}
write_examples(skill, examples, config) -> path
read_examples(skill, config) -> {language: [phrase]}
needs_enrichment(skill, config, languages) -> boolean
```

The publish-time contract has three parts.

**Declared truth.** The triggers in the skill file are the only authored
examples. Everything beside them is derived from them, is never edited by hand,
and can be thrown away and produced again. A catalog with every derived file
deleted still routes exactly as its declarations say; a derived file carries no
authority of its own.

**Source hash.** A derived file records a hash of the declared triggers it was
made from. When the triggers are edited the hash stops matching and the file is
ignored in full — never partly used, never repaired in place. An edited trigger
can therefore lose paraphrases, but can never keep wrong ones, and the fix is
always another pass rather than a correction by hand.

**Derived files.** The file lives in the version directory of the skill it
describes, so it is copied, superseded and removed with that version. An index
that consumes derived files must notice when one is written: a fingerprint over
the skill files alone does not change when a file beside them does.

Derived phrases are indexed after the declared texts and are bounded by a cap
per procedure; when the cap bites it is a derived phrase that gives way, never a
declared one. Because a procedure scores the best of its texts and not their
average, additional phrases can raise a score and cannot dilute one. What a
ranking matched on is reported with the language the winning phrase was derived
for, so a surprising route can be read back and explained rather than guessed
at.

### The words people actually used

Responsibility: let a procedure be reached by the wording its traffic shows,
without anyone editing what its author wrote.

A scheduled job in the evolution layer reads the turns that reached a procedure
and finished, and keeps the phrasings that say something the declarations do not
already say. It writes them to one file per procedure under the state directory,
never inside a skill directory:

```text
LearnedPhrases:
  phrases(skill) -> [SkillText]           # read back, never written here
  fingerprint(skills) -> string
```

They are indexed last: after the declared texts, after the derived phrases, and
under the same cap per procedure, so the cap drops a learned phrase before a
derived one and a derived one before anything declared. A ranking names what it
matched on and says which of the three won it — a declared text as written, a
derived phrase with the language it was derived for, a learned phrase as
learned — so a route nobody expected can be read back to the sentence that
caused it.

An index folds these files into the fingerprint it holds, exactly as it does the
derived ones, because the registry fingerprints the skill files alone. Writing a
learned file rebuilds the index, and so does deleting one, which is what makes
the deletion the whole of the undo: the ranking after a revert is the ranking
from before it. Nothing in this layer writes these files, and reading them costs
no model call.

### Language of a turn

The language a reply is written in is a fact of the session, not a property of
the message. The routing decision may report the language of the turn; when it
does, the session records it, and every later turn is answered in it until a
decision reports something else. A turn the rules or the fast path decided
reports no language and re-detects nothing: the session already knows. The
runtime's configured default applies only to a session nothing has yet settled.

This is why routing in another language and replying in another language are one
mechanism and not two. Discovery reaches the procedure through the derived
phrases; the reply comes back in the language the session settled on.

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

#### What the turn leaves on offer

An offered action is a key and a label. The key names the action and never
reaches a person; the label is the wording its author declared, in whatever
language the catalog is written in. A reply is written in the language the
person used, so the two can disagree, and a turn that answers in one language
and offers its next steps in another has failed at something small and visible.

They are reconciled by the call that is already being made. The reply writer
receives the actions with the reply brief and may return a label for each key
in the language it is answering in; the runtime uses a returned label in place
of the declared one and keeps the declared wording where nothing came back.

```text
reply brief:
  reply_intent, outcomes, actions: [{key, label}]
  presentation_preferences: [string]     # optional; how the answer should read

reply answer:
  reply: string
  action_labels: {key: string}           # optional; the wording, translated
```

The offer itself is then stated by the runtime, one action per line, after the
reply and after the receipt lines. There is no sentence around the list,
because a lead-in would be written in the language of the implementation and
would sit in front of labels written in the language of the conversation.

Three things follow from doing it this way. Nothing is added to the turn: the
same three calls a turn already makes. The prompts routing is built from stay
in the language the catalog declares, because routing is asked in every
language a person writes in and must not be narrowed to one. And an action is
never invented by rewording: a label returned under a key the turn did not
offer is dropped.

A presentation preference recorded for the session, if there is one, travels in
the same brief as a fact about how the answer should read. It goes no further.
The planner is not told it, no policy reads it, and no payload carries it; what
a turn does is decided by the catalog and the plan, never by a preference
about prose.

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
- A missing provider degrades the system to catalog-derived answers; it never
  degrades governance.
- No feature adds a model call to a turn. Work that needs a model happens at
  publish time or in a scheduled job, and a turn reads what that work left.
- The declared triggers are the only authored examples; everything derived from
  them is regenerable and is ignored as soon as it stops matching them.
- The language of a session is settled by a routing decision and kept; it is not
  detected again on a turn that never reached the model.

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
  catalog.
- **OSKA-COG-11** — A conformant system MUST provide a scripted provider that
  satisfies the provider interface, and MUST be able to run its entire
  conformance suite with it, offline.
- **OSKA-COG-12** — A feature MUST NOT add a model call to a turn. Work that
  needs a model MUST be performed at publish time or by a scheduled job, and a
  turn MUST NOT exceed the configured budget of model calls.
- **OSKA-COG-13** — Routing examples derived from the declared triggers MUST be
  produced outside a turn. A turn MAY read them and MUST NOT write them.
- **OSKA-COG-14** — The declared triggers MUST remain the only authored routing
  examples. Derived phrases MUST NOT be edited by hand and MUST be reproducible
  from the declarations alone.
- **OSKA-COG-15** — A derived file MUST record a hash of the declared triggers
  it was derived from, and MUST be ignored in full when that hash no longer
  matches the declarations.
- **OSKA-COG-16** — Derived phrases MUST be indexed after the declared texts and
  MUST be bounded by a configured cap per procedure; the cap MUST NOT drop a
  declared text.
- **OSKA-COG-17** — A ranking MUST report the text it matched on, and MUST
  distinguish a derived phrase from a declared one by naming the language the
  phrase was derived for, and a learned phrase from both by naming it as
  learned.
- **OSKA-COG-17a** — Phrasings learned from traffic MUST be indexed after the
  declared texts and the derived phrases, under the same cap per procedure, and
  MUST be read from the state directory and never from a skill directory. An
  index MUST rebuild when one of their files is written or deleted.
- **OSKA-COG-18** — A failure to derive routing examples MUST NOT fail the
  publication that occasioned it, and MUST be reported.
- **OSKA-COG-19** — The language of a reply MUST be a fact of the session,
  settled by a routing decision that reports one and kept until another decision
  reports a different one. A turn decided by the rules or by the fast path MUST
  NOT detect it again.
- **OSKA-COG-20** — The actions a turn leaves on offer MUST reach the reply
  writer as a key and the label its author declared, and the reply writer MUST
  be able to return a label for each key in the language of the reply. The
  runtime MUST use a returned label in place of the declared one, MUST keep the
  declared wording for an action no label came back for, and MUST discard a
  label returned under a key the turn did not offer.
- **OSKA-COG-21** — Rewriting those labels MUST travel in the call that writes
  the reply and MUST NOT add a model call to the turn, in accordance with
  `OSKA-COG-12`. A conformant system MUST be able to show this by counting the
  completions of a turn that offers actions against those of a turn that offers
  none.
- **OSKA-COG-22** — The prompts a routing decision is built from MUST stay in
  the language the catalog declares, whatever language the turn is answered
  in.
- **OSKA-COG-23** — What a turn offers MUST be stated from the labels it holds
  and MUST NOT be surrounded by text the runtime writes in a language of its
  own.
- **OSKA-COG-24** — A presentation preference held for a session MUST reach the
  reply writer as a fact and MUST NOT reach the planner, a policy or an
  operation payload.

## Rationale

The obvious alternative is the agent loop: give the model the tools, let it
call them, observe, and repeat until it decides it is done. It is more capable
in the open case and it is the wrong shape here. The loop has no point at which
the intended effect exists as a complete object, so there is nothing to freeze,
nothing to hash, and nothing to show a human before the effect occurs. Approval
in a loop is approval of the next call, asked repeatedly, which people learn to
click through. A plan produced in one step, validated in one step and executed
under governance gives approval a payload to attach to.

Splitting routing into rules, fast path and model is not an optimization, or
not only one. The rules stage exists because some classifications must not
depend on a model at all: whether the word "yes" approves a pending write is a
question about session state, and a model that gets it wrong approves something
nobody approved. The fast path exists because most turns are one of a handful
of known requests, and a lexical match against declared triggers is both
cheaper and more predictable than a classification call. The model stage
handles what is left, which is the interesting part.

Constraining the reply writer to stated outcomes is the least glamorous rule
here and the one that changes the system's character most. A model asked to
summarize what happened will smooth over a failed read-back, because fluent
prose prefers a clean ending. Handing it a typed outcome list, and forbidding
anything outside it, is what makes "the restart is unverified" a sentence the
system can actually produce. The conformance suite checks this directly: no
reply may call a write verified unless its receipt says so.

## In the reference implementation

- `src/oska/index/` — `base.py`, `keyword.py`, `embedding.py`,
  `discovery.py`.
- `src/oska/catalog/enrichment.py` — deriving, writing and reading the
  routing examples, and the fingerprint that tells an index one was written.
- `src/oska/index/base.py` — the texts a procedure is indexed under, the cap
  that declared texts survive, the label a ranking reports, and the learned
  phrasings read back from the state directory with their own fingerprint.
- `src/oska/cli/commands_enrich.py` — asking for a pass over the catalog and
  reading back what a procedure is indexed under.
- `src/oska/router/` — `base.py` (rules), `fast_path.py`,
  `model_router.py`.
- `src/oska/planner/` — `provider.py` (the interface), `scripted.py`, the
  service-backed provider implementation, `planner.py`, `prompts.py`.
- `src/oska/engine/reply.py` — the deterministic renderer used when no
  provider is configured, the lines that state what a turn leaves on offer, and
  the presentation preferences handed to the reply writer and to nothing else.
- `src/oska/catalog/resolver.py` — the offered action as a key and a label,
  the rule that two actions never share a key, and the relabeling that keeps
  the declared wording where nothing came back.
- `src/oska/planner/planner.py` — the reply schema carrying `action_labels`,
  and the brief the reply writer answers from.
- `tests/unit/test_labels.py` — the offer in the language of the turn, the
  count that shows no call was added, and the preference that reaches the reply
  writer and no other prompt.
- `examples/ops/scenarios/26_labels_in_the_turn_language.json` — the same
  thing as an executable document: a turn asked in Italian, offered its next
  steps in Italian, in three model calls.
