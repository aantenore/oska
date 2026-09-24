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
| `GET /health` | Whether the runtime is up, and which catalog it holds |

Only `oska run` and `POST /sessions/{session_id}/turns` carry turns; the
rest read the catalog or drive the evolution layer. Neither channel
authenticates: both assume an authenticated caller in front of them, and the
actor of a proposal transition is recorded as it arrives.

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
   procedure runs. A model provider, if present, only phrases it.
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
8. **Advance the goal.** While a step remains: hydrate the current procedure and
   emit `hydrate`; with no model provider, stop and say so without running
   anything; otherwise plan it and emit `plan`. A plan that does not validate
   emits `plan` as invalid, records `invalid_plan` and discards the goal. If the
   plan reports missing inputs, store them, ask the first question and stop.
   Otherwise execute the plan from its first operation. If execution stops for
   an approval, reply with the exact payload and stop. If it stops for any other
   reason, discard the goal. When a step completes, move to the next; when all
   steps complete, discard the goal.
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
    current state. Re-evaluate the policy, emitting `policy` for that second
    evaluation; a denial records `denied` and stops here. Record the intent,
    execute, verify, update the receipt, record a `receipt` outcome, then
    continue the same plan from the operation after the write. When that plan
    completes and the goal has further steps, move to the next step and resume
    at step 8. Never re-plan on resume.
11. **Suggest.** When a read-only procedure completes, ask the resolver which
    actions the entities just read support, and carry those phrases as
    suggestions.
12. **Reply.** Build the reply from the outcome list, the pending information,
    the suggestions and the reply intent, in the session language. Emit a
    `reply` event.
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

- `src/oska/engine/` — `engine.py` (the algorithm), `execution.py` (plan
  execution and the write path), `goals.py`, `state.py`, `session_store.py`,
  `outcomes.py`, `reply.py`, `factory.py`.
- `src/oska/contracts/common.py` — `TurnResult`, `Outcome`, `PendingGoal`,
  `PendingApproval`, `PendingInfo`.
- `src/oska/services.py` — the one assembly both channels are built over:
  engine, catalog, proposal store, publication root.
- `src/oska/cli/` (`main.py`, `commands_run.py`, `commands_serve.py`,
  `commands_propose.py`, `options.py`) and `src/oska/api/` (`app.py`,
  `routes.py`) — the two channels over that assembly.
