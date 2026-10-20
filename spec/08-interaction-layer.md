# 08 Interaction layer (L6)

## Purpose

L6 runs the conversation. It owns session state, executes the turn algorithm,
carries the human-in-the-loop points, and is the only layer that both consults
cognition and drives governance. Everything a user experiences — being asked a
question, being shown a payload, being told the same request already ran — is
decided here.

## Modules

### Session

Responsibility: remember what one conversation is in the middle of.

```text
SessionState:
  session_id: string
  language: string
  revision: string               # the catalog revision this session is served by
  pending_goal: PendingGoal | null
  pending_approval: PendingApproval | null
  last_entities: [string]        # entities the most recent read touched
  presented: object              # what the runtime last showed, from evidence
  focus: object                  # what the conversation is about, derived from it
  history: [Message]
  receipts: [receipt_id]
  turn_count: integer

PendingGoal:
  procedure_ids: [string]        # one entry, or a composite's members in order
  step_index: integer
  collected_inputs: object
  missing: [{field, question}]
  request_text: string
```

State is saved at the end of every turn, whatever the turn did, including turns
that failed.

`revision` is empty where the catalog is served as a directory, because there
is nothing to name. Where it is served as a revision, the session records the
one it was served by on its first turn and reads the whole catalog through it
for as long as the task lasts.

### Pinning and adoption

A catalog that changes under a conversation rewrites what has already been
said. A procedure retired between the moment an approval was requested and the
moment it was answered takes with it the sentence the person is looking at; the
actions offered in a turn already composed stop existing; a plan held for
approval names an operation the catalog no longer declares. None of that is
wrong for a demonstration and all of it is wrong for a person holding a pending
approval.

The rule is therefore that a session is served by one revision at a time, and
that the revision changes only where changing it cannot rewrite anything:

- A session with no revision takes the one the deployment serves now.
- A pinned session keeps its revision for every turn of the task it is in the
  middle of, whatever has been published since.
- A turn that starts a new task takes the newest revision, records it on the
  session and emits a `revision` event naming the one it left and the one it
  took. Nothing of the previous task survives that turn — a pending approval is
  cancelled by starting a new task in any case — so there is nothing left to
  rewrite.
- A deployment that switches pinning off still holds a session to its revision
  while something of the task is waiting to be answered under it: an approval,
  or a goal that stopped to ask a question. A question is the same promise in a
  smaller form — the procedure the answer completes is the one the person was
  asked for — so the answer is resolved through the revision the question was
  asked under. Where pinning is off and nothing is waiting, the next turn takes
  the newest revision whether or not it starts a new task.

The last of those is the answer to what happens to an approval that outlives the
catalog it was requested under: it is executed under that same revision, not
refused and not re-read against the newer one. The approval is frozen on the
hash of a payload that was composed from a procedure of that revision and
described to a person in that revision's words. Running it against a newer
catalog would run something other than what was described, and refusing it
would throw away a decision a person has already made for a reason they cannot
see. The revision is immutable and still in the store, so honoring it costs
nothing. The checks that already stand do not move: the payload is re-hashed,
the policy is re-evaluated against current state, and a write the revision does
not declare still stops. What the pin removes is only the case where the
catalog moved underneath.

Adoption is deliberately narrow. Routing a turn happens before the engine knows
whether the turn starts a new task, so it is decided under the revision the
session held; the procedure it names is looked up again after adoption, and a
revision that no longer publishes it answers from the declared scope. That is
what a retired procedure should do to a request that arrives after it was
retired, and it is the reason adoption need not reach back into routing.

### Session Store

Responsibility: persist and load session state by identifier, durably enough
that a restart between two turns is invisible to the user.

```text
SessionStore:
  load(session_id) -> SessionState
  save(state) -> void
```

### Turn Engine

Responsibility: execute the algorithm below, emit the events, and return one
typed result.

```text
TurnResult:
  reply: string
  outcomes: [Outcome]
  pending: {kind: input|approval, question, approval} | null
  events: [AuditEvent]
  directives: [Directive]        # empty unless the domain enables presentation

Directive:
  procedure: string
  moment: on_results | on_single_result | on_selection
        | on_verified_write | on_completion
  view: string
  fields: object

Outcome:
  kind: read | receipt | ledger_hit | denied | rejected
      | invalid_plan | approval_pending | cancelled
  integration_id, operation, data, receipt_id, reason
```

### Channels

Responsibility: carry turns in and results out. A command line, an HTTP
endpoint, a chat adapter. A channel adds no behavior: it maps a transport onto
`turn(session_id, text)` and renders the result. Two channels over the same
engine MUST behave identically.

A channel is built over one assembled runtime — one engine, one catalog, one
proposal store — so that what a turn does over one transport and what it does
over another cannot drift apart.

The reference implementation carries two channels. The command line:

| Command | What it does |
| --- | --- |
| `oska validate` | Check every skill in the catalog and report what is wrong |
| `oska config` | Print the settings in force and the configuration files they name, as JSON, with secrets masked |
| `oska capabilities` | Print the procedures on offer, in the words the catalog declares |
| `oska ontology` | Print the declared entities and what acts on them, as a summary, as JSON or as a diagram |
| `oska run` | Run one turn, or read turns until the input ends |
| `oska receipts` | Print every write receipt of one session, oldest first |
| `oska serve` | Serve the HTTP application |
| `oska conform` | Run a scenario directory and report the level it reaches |
| `oska propose from-session` | Draft a candidate procedure from what one session did |
| `oska propose list` | Print every proposal held |
| `oska propose show` | Print one proposal in full |
| `oska propose transition` | Move one proposal to another state, recording who asked |

The HTTP application:

| Route | What it does |
| --- | --- |
| `POST /sessions/{session_id}/turns` | Run one turn of one session |
| `GET /sessions/{session_id}/receipts` | Every write receipt of one session |
| `GET /skills` | The effective version of every catalog entry |
| `GET /skills/{skill_id}` | One catalog entry, frontmatter and sections |
| `GET /capabilities` | The declared scope and the procedures on offer |
| `POST /proposals/from-session/{session_id}` | Draft a candidate procedure from a session |
| `GET /proposals` | Every proposal held |
| `GET /proposals/{proposal_id}` | One proposal |
| `POST /proposals/{proposal_id}/transition` | Move one proposal to another state |
| `GET /changes` | Every change request held |
| `GET /changes/{request_id}` | One change request |
| `GET /health` | Whether the runtime is up, and which catalog it holds |

Only `oska run` and `POST /sessions/{session_id}/turns` carry turns; the
rest read the catalog or drive the evolution layer.

The HTTP channel resolves every call to an identity before its handler runs,
in one of two modes the configuration chooses. In `proxy` mode an
authenticating proxy in front of the runtime sets the subject and attributes
headers and the runtime takes them as given, so the binding of a write to the
person who asked is exactly as strong as the deployment's guarantee that
nothing reaches the port except through that proxy. In `token` mode the caller
presents a signed session token and the channel verifies it itself
([06-governance-layer.md](06-governance-layer.md)). With `identity.required`,
on by default in `token` mode, a call without an identity is refused with 401;
the health read stays open, and the metrics read only where the configuration
marks it public. A deployment that requires no identity answers a call that
carries none as a conversation, and serves it as its single user, review
included, only where it declares itself single-user.

A call on a session — a turn, its receipts, a proposal drafted from it — is
held to the subject that opened the session, and every refusal of such a call
answers the same 403, whatever its reason. Reading proposals and change
requests and moving a proposal need a principal whose `roles` attribute holds
the configured reviewer role; drafting from a session is open to the session's
own principal and to a reviewer. A transition is recorded under the caller's
principal whatever its body says; only a call without an identity, in a
single-user deployment, names its actor in `by`, and that name is recorded as stated and
marked unverified. The command line records the person at the terminal as the
operator. What the reference channel reads and refuses is in
`src/oska/api/identity.py` and `src/oska/api/routes.py`.

### Presentation directives

Responsibility: tell a presentation surface what a turn may show, in a form a
channel renders without interpreting the conversation.

A procedure declares, per moment of a turn, a view and how its fields are
filled:

```text
presentation:
  <moment>:
    view: string
    bind: {field: binding}     # "$items.<f>" | "$item.<f>" | "$selection"
                               # | "$receipt.<f>" | literal
    fields: object             # copied as written
```

After a turn, each procedure that ran reaches the first of these moments it
declares: `on_verified_write` (one of its writes was performed and verified),
`on_selection` (its subject was resolved from the focus and a read of the turn
returned that record), `on_results` (a read returned several records),
`on_single_result` (a read returned one), `on_completion` (its goal finished
in the turn and every write of the turn is executed and verified). Every
binding is answered from evidence the runtime holds for that procedure: the
records its reads returned, the record the turn selected, and a receipt whose
read-back verified it. What a person typed is not evidence: a value somebody
gave is shown once a write carried it and its read-back confirmed it, as
`$receipt.<field>`, and a binding to `$inputs.<field>` is refused when the
catalog is read, under `presentation_unverified_input`. A binding the evidence
cannot answer drops the whole directive. No value is taken from model text.

The contract with a channel is narrow. A directive is a request to show
something: a channel MAY render it and MAY ignore it, and nothing a channel does
with it reaches back into the turn. A directive never writes, never approves
and is never read into a plan, a policy or a payload. It is emitted only where
the serving domain enables presentation; elsewhere a turn returns none. The
directives of a turn are also recorded on its `reply` audit event.

### Human-in-the-loop

Responsibility: the three points where the system stops and waits — a question
for a missing input, an approval for a frozen payload, and a cancellation that
discards both. Each is a state on the session, not a callback, so a restart
between the question and the answer changes nothing.

## The turn algorithm

One user turn. Numbered steps, executed in order.

1. **Load state.** Load the session, increment the turn count, settle the
   language from state or the configured default, settle the catalog revision
   this turn reads through and emit a `revision` event if it differs from the
   one the session carried, emit a `turn` event and append the user message to
   the history.
2. **Route.** Run the router with a context holding whether a goal is pending,
   whether an approval is pending, and the session language. Emit a `route`
   event with the kind, the procedure, the confidence, the deciding stage and
   the language. If the decision carries a language, adopt it.
3. **Greeting or out of scope.** Reply from the resolver's declared scope. No
   procedure runs. A model provider, if present, only phrases it: an out of
   scope reply is written by the one reply call of the turn from the scope
   handed over as a fact, and falls back to the template.
4. **Cancellation.** Discard any pending approval — emitting
   `approval_decided` with decision `cancelled` — and any pending goal. Record a
   `cancelled` outcome.
5. **Approval or rejection.** With nothing pending, treat the turn as out of
   scope: a bare "yes" approves nothing. On rejection, emit `approval_decided`
   with decision `rejected`, discard the approval and the goal and record
   `rejected`. On approval, re-hash the frozen payload; a mismatch emits
   `approval_decided` with decision `cancelled`, records `cancelled` and
   discards both. Otherwise increment the approvals given and emit
   `approval_decided` with decision `approved` and the counts; if more are
   required, record `approval_pending`, restate the request and stop. When the
   last approval arrives, go to step 10.
6. **Answering a question.** If the turn provides input or continues a task and
   a goal is pending with no approval pending, store the answer under the field
   of the first outstanding question, drop that question, and go to step 8.
   With no pending goal, or with an approval pending, treat the turn as a new
   task.
7. **New task.** Cancel any pending approval, recording it. Take the newest
   revision the deployment serves, recording it on the session and emitting a
   `revision` event if it is not the one the turn opened on. Take the
   procedure the router named, or ask discovery; on a tie, ask the planner to
   pick. If nothing is chosen, reply out of scope. Set the pending goal: a
   composite expands into its members in order, anything else is a single step.
   A member whose subject joins the focus starts with the record the message
   refers to already collected, resolved in the precedence of L5.
8. **Advance the goal.** While a step remains: hydrate the current procedure and
   emit `hydrate`; with no model provider, stop and say so without running
   anything; otherwise plan it and emit `plan`. A plan that does not validate
   emits `plan` as invalid, records `invalid_plan` and discards the goal. If the
   plan reports missing inputs, store them, ask the first question and stop.
   Otherwise execute the plan from its first operation. If execution stops for
   an approval, reply with the exact payload and stop. If it stops for any other
   reason, discard the goal. When a step completes with a write among its
   outcomes that is not executed and verified, a ledger hit included, discard
   the goal and run no later step. When a step completes, move to the next;
   when all steps complete, discard the goal.
9. **Execute a plan.** For each operation in order: look up its declaration; an
   undeclared operation is an `invalid_plan` outcome and stops the plan. A read
   executes immediately, records a `read` outcome and updates the entities seen
   this turn. A write enters the write path of L4: capability check, ledger
   lookup, policy, approval. A ledger hit records the original receipt and
   continues. A denial records `denied` and stops. An allowed write freezes its
   payload, records `approval_pending` and stops the turn.
10. **Resume after approval.** Hydrate the procedure the approval names; a
    procedure the catalog no longer holds records `invalid_plan` and stops.
    Re-read what the plan read before the write, so the policy is judged on
    current state; a read that cannot be performed again records
    `unavailable`, saying the state could not be confirmed, and stops here with
    nothing sent. Re-evaluate the policy, emitting `policy` for that second
    evaluation; a denial records `denied` and stops here. Record the intent,
    execute, verify, update the receipt and record a `receipt` outcome. A
    write that is not executed and verified ends the goal here: nothing after
    it runs. Otherwise continue the same plan from the operation after the
    write. When that plan completes with every write executed and verified and
    the goal has further steps, move to the next step and resume at step 8.
    Never re-plan on resume.
11. **Suggest.** When a read-only procedure completes, ask the resolver which
    actions the entities just read support, and carry those phrases as
    suggestions.
12. **Reply.** Record what the turn showed as `presented` and derive `focus`
    from it. Where the domain enables presentation, build the directives of
    the procedures that ran. Build the reply from the outcome list, the pending
    information, the suggestions and the reply intent, in the session
    language. The reply writer is told, for each write of the turn, whether it
    landed. When any write of the turn is not executed and verified, the text
    the reply writer returns is set aside and the deterministic form answers.
    The deterministic form is said in the session language from the phrase
    catalog, falling back phrase by phrase to the default language where a
    phrase has no translation; the offered actions keep the labels the call
    returned. Emit a
    `reply` event carrying the directives and any record the
    turn took from the focus.
13. **Save.** Append the reply to the history and save the session, whatever
    happened.

## Invariants

- State is saved on every turn, including turns that raised.
- A session reads one catalog revision for the whole of one task.
- An approval is executed under the revision it was requested under, or not at
  all.
- A goal that asked a question is continued under the revision it asked it
  under.
- Approvals are per session. An approval in one session approves nothing in
  another.
- A new goal cancels a pending approval, and the cancellation is recorded.
- While an approval is pending, a turn is never treated as providing input.
- An identical write request in the same session returns the original receipt
  instead of writing again.
- A composite runs its members in order; each member keeps its own policy, so a
  write inside a composite still stops for approval.
- Two channels over the same engine produce the same outcomes for the same
  turns.
- The engine, not the model, owns the transition of session state.

## Requirements

- **OSKA-IXN-1** — Session state MUST be persisted at the end of every turn and
  restored at the start of the next, so a restart between turns is invisible.
- **OSKA-IXN-2** — The turn engine MUST execute routing, planning, execution and
  reply in the order given above, and MUST emit an audit event at each named
  step.
- **OSKA-IXN-3** — An approval or rejection turn with nothing pending MUST NOT
  execute anything, and MUST answer from the declared scope.
- **OSKA-IXN-4** — A `new_task` turn while an approval is pending MUST cancel
  that approval, record a `cancelled` outcome, and execute nothing from the
  cancelled plan.
- **OSKA-IXN-5** — While an approval is pending, a turn MUST NOT be treated as
  providing input to a goal.
- **OSKA-IXN-6** — A new goal MUST replace a pending goal; the engine MUST NOT
  merge two goals.
- **OSKA-IXN-7** — Missing inputs MUST be asked one question at a time, and the
  answer MUST be stored against the field that question named.
- **OSKA-IXN-8** — Resuming after approval MUST continue the stored plan from
  the operation after the approved write, and MUST NOT re-plan.
- **OSKA-IXN-9** — A composite MUST expand into its member procedures in
  declared order, each planned and executed under its own policy.
- **OSKA-IXN-10** — An identical write request in the same session MUST return
  the original receipt and MUST NOT reach a connector.
- **OSKA-IXN-11** — Approvals MUST be scoped to the session that requested
  them.
- **OSKA-IXN-12** — Every turn MUST return a typed result carrying the reply,
  the outcomes, the pending state and the events; a channel MUST NOT add
  behavior beyond rendering it.
- **OSKA-IXN-13** — When no model provider is configured, the system MUST still
  answer from the catalog and MUST NOT execute any procedure that requires a
  plan.
- **OSKA-IXN-14** — Where the catalog is served as a revision, a session MUST
  record the revision it is served by on its first turn, and that record MUST be
  part of the session state that survives a restart.
- **OSKA-IXN-15** — While session pinning is in force, every turn of a session
  MUST resolve skills through the revision the session records, even when the
  deployment serves a newer one.
- **OSKA-IXN-16** — A turn that starts a new task MUST adopt the revision the
  deployment serves, MUST set it on the session, and MUST emit a `revision`
  audit event naming both the revision left and the revision taken.
- **OSKA-IXN-17** — An approval requested under one revision MUST be executed
  under that revision. It MUST NOT be re-resolved against a newer one, and the
  re-hash of the frozen payload and the second policy evaluation MUST still
  apply.
- **OSKA-IXN-18** — A deployment that does not pin sessions MUST still resolve a
  turn that answers a pending approval through the revision that approval was
  requested under.
- **OSKA-IXN-19** — Every receipt MUST name the revision in force for the turn
  that opened it, where the catalog is served as a revision.
- **OSKA-IXN-20** — A deployment that does not pin sessions MUST also resolve a
  turn that answers a question a goal asked through the revision that goal was
  planned under, and the goal MUST finish under that revision. A session MUST
  take a newer revision only on a turn where nothing of an earlier turn is
  waiting to be answered.
- **OSKA-IXN-21** — A turn MUST return a directive only where the serving domain
  enables presentation, and only for a procedure that ran in the turn and
  declares the moment it reached. A turn in a domain that does not enable it
  MUST return none.
- **OSKA-IXN-22** — Every field of a directive MUST be copied as declared or
  answered from evidence the runtime holds: the records the procedure's reads
  returned, the record the turn selected and a read returned, or a receipt
  whose read-back verified it. A binding the evidence cannot answer MUST drop
  the whole directive, and a field MUST NOT be taken from model text.
- **OSKA-IXN-23** — A directive MUST NOT write, approve or reach a plan, a
  policy or a payload. A channel MAY render or ignore it, and nothing a channel
  does with it MUST change what the turn did.
- **OSKA-IXN-24** — The directives of a turn MUST be recorded on the audit
  trail of that turn.
- **OSKA-IXN-25** — A binding MUST name one of `$items.<f>`, `$item.<f>`,
  `$selection` or `$receipt.<f>`. A binding to what a person typed
  (`$inputs.<f>`) MUST be refused (`presentation_unverified_input`).
- **OSKA-IXN-26** — `on_completion` MUST be reached only when the goal finished
  in the turn and every write of the turn is executed and verified.
- **OSKA-IXN-27** — The reply writer MUST be told, for each write of the turn,
  whether it landed. When any write of the turn is not executed and verified, a
  model-written reply MUST NOT be used; the deterministic form MUST answer.
- **OSKA-IXN-28** — Every sentence the runtime says in its own words MUST come
  from a phrase catalog keyed by language, in which the default language is
  complete. A phrase missing in the language of the session MUST fall back to
  the default language for that phrase alone. Values (operation names,
  integration identifiers, payloads, receipt identifiers, reasons) MUST be
  inserted verbatim. A translation that names placeholders other than those of
  its default-language phrase MUST be refused when the catalog is loaded. The
  words an approval request tells a person to type MUST be accepted by the
  approval rules in every language the deployment declares it answers in, or
  the catalog MUST be refused when it is loaded. A reason the runtime gives in
  its own words for dropping a request or holding a write back is such a
  sentence; a reason written by a policy, a plan check or a connector is a
  value. A catalog MAY be laid in layers, each over the ones before it phrase
  by phrase and language by language (the shipped catalog, then the
  deployment's, then those of the packs a domain serves); every layer MUST be
  held to the checks above, and two packs of one domain that give one phrase in
  one language differently MUST be refused.
- **OSKA-IXN-29** — The conformance check for a reply that calls a write
  verified when its receipt is not MUST read the words that make that claim, and
  the words that negate it, from the same phrase catalog, in every language the
  catalog holds.

## Rationale

The obvious alternative is to keep conversational state in the message history
and let the model reconstruct it. Ask the model what the user is waiting for;
it has the transcript. This works until the transcript and the truth disagree,
which is exactly the moment it matters. A message saying "approve the restart
of search" is in the history whether or not an approval is outstanding, so a
later "yes" is indistinguishable from an approval unless something outside the
transcript knows. Holding the pending approval as typed state makes that
question a lookup rather than an inference, and makes `OSKA-IXN-3` — a bare
"yes" with nothing pending executes nothing — enforceable.

The rule that a new goal replaces rather than merges is a deliberate refusal of
a nicer-sounding behavior. Merging looks helpful: the user asked to restart a
service and then asked for its logs, so do both. It is also how an approval
gets carried onto a payload the user never saw, because the approved plan and
the new intent become one object. Replacement is blunt and it is legible: the
old plan is cancelled, the cancellation is recorded, and the new request starts
from nothing.

Not re-planning on resume comes from the same instinct. Between the approval
request and the approval, minutes may pass. Re-planning would produce a plan
built after the user read the payload, and the thing executed would no
longer be the thing approved. So the plan is stored with the approval and
resumed from the index after the write, while the *policy* — not the plan — is
re-evaluated against fresh reads. The payload is fixed; the judgment about
whether it may run is current.

## In the reference implementation

- `src/oska/engine/` — `engine.py` (the algorithm and turn dispatch),
  `execution.py` (plan execution), `approvals.py` (approve and reject, the
  hash checks, the second evaluation, going on only past a verified write),
  `settlement.py` (the claim, the send and the read-back), `binding.py` (the
  identity and the revision a session is bound to), `budget.py`,
  `boundary.py`, `collaborators.py`, `principal_inputs.py`, `lifecycle.py`,
  `calls.py`, `context.py`, `goals.py`, `state.py`, `session_store.py`,
  `outcomes.py`, `reply.py`, `factory.py`.
- `src/oska/contracts/common.py` — `TurnResult`, `Outcome`, `PendingGoal`,
  `PendingApproval`, `PendingInfo`, `Directive`.
- `src/oska/engine/presentation.py` — the moments, the evidence of each
  procedure, the binding of a declared moment to it, and the switch that keeps
  a domain without presentation from emitting any.
- `src/oska/ontology/naming.py` and `src/oska/catalog/checks/presentation.py`
  — the binding grammar, and the refusal of a binding to what a person typed.
- `tests/unit/test_presentation.py` and
  `examples/ops/scenarios/28_directive_from_verified_write.json` — a directive
  filled from a verified receipt, one dropped when a binding cannot be
  answered, none when presentation is off, and no completion view past a
  write that did not land.
- `src/oska/engine/phrases.py`, `configs/phrases.json` — the phrase catalog:
  the phrases the code asks for, the layered load, the placeholder check and
  the check of the approval words against the approval rules
  (`OSKA-IXN-28`); `src/oska/conformance/runner.py` reads its claim words from
  it (`OSKA-IXN-29`). `tests/unit/test_phrases.py` and
  `examples/ops/scenarios/20_other_language.json` — the approval request and
  the receipt line in Italian, and an Italian reply that overstates a write
  caught by the conformance check; the same for Spanish, French and German.
  `src/oska/platform/assembly.py` (`domain_phrases`) — a pack's `phrases.json`
  checked and laid over its domains' catalog, and two packs giving a phrase
  differently refused (`tests/unit/test_platform.py`, `tests/unit/test_kit.py`).
- `src/oska/services.py` — the one assembly both channels are built over:
  engine, catalog, proposal store, publication root.
- `src/oska/cli/` (`main.py`, `commands_run.py`, `commands_serve.py`,
  `commands_propose.py`, `options.py`) and `src/oska/api/` (`app.py`,
  `routes.py`) — the two channels over that assembly.
