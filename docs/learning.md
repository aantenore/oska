# Learning

A session that did something worth repeating can become a procedure. It becomes
one the same way a hand-written procedure does: as a file in a catalog root that
passes the validator. What the learning layer adds is a drafting step and a
lifecycle with a named actor on every move.

## Proposals

`draft_from_session` reads three things: the conversation history of a session,
the write receipts the session produced, and the catalog. From the receipts it
takes the operations that ran, the integrations they belong to, the entities
those integrations declare, and the payload fields that were carried. From that
it builds a complete skill file, and a policy to go with it.

The result is a `SkillProposal` with a proposal id, the state `draft`, the
session it came from, the rendered Markdown, the policy document, a quality
report, and a history whose first entry records the draft.

The drafted skill is deliberately narrow:

- `type: runbook`, `version: 1`, `status: draft`.
- The id is derived from the first write operation, slugged.
- `capabilities.writes` holds exactly the operations the session ran. Nothing
  else is added.
- `policy: ./policy.json`, and the policy asks for a second approval on every
  one of those writes, with `max_per_session: 1`, and the reason `Recorded from
  a session and not reviewed yet.` A learned procedure is the most conservative
  thing in the catalog until someone edits it.
- The body is filled in from a template: what ran, in order, followed by a
  verification section saying the read-back is the evidence, and a rollback
  section that says the session recorded no way to undo this and a reviewer must
  state one.

With a model provider, the wording is written from the conversation instead of
the template — description, triggers, the runtime action, and the five sections
— against a schema and a short list of rules: describe only what the recorded
operations and the conversation show, never invent an operation or a value, give
at least three distinct triggers, and the response to a write is not evidence.
The model can only replace text. The operations, the integrations, the entities
and the policy come from the receipts either way, so a model that writes
something imaginative still cannot widen what the procedure may do.

Triggers are filtered against what published runbooks already claim, so a draft
does not steal an existing procedure's phrasing.

## Quality checks

`quality.check` renders the proposal to a temporary directory, with the policy
written beside it, and runs three things:

1. **Parse.** The frontmatter must produce a valid `SkillFrontmatter`. A parse
   failure is the only result — nothing else could run — and the score bottoms
   out.
2. **Validate.** Every catalog rule in [skill-contract.md](skill-contract.md),
   against the live catalog. That is where an undeclared write, an unknown
   integration or a broken policy is caught.
3. **Lints.** Six checks the catalog itself does not make: the description is
   between 20 and 200 characters; at least three distinct triggers; every
   declared write is a write declared by a listed integration; the `Steps`
   section is not empty; the `Verification` section is not empty; no trigger
   already belongs to another published runbook.

The report carries the list of issues and a score between 0 and 1, computed as
`1 - issues / (lints + 1)` and clamped, so a clean proposal scores 1 and one
that fails a parse scores 0. `quality.passed` is true only when the issue list
is empty. The score is information for a reviewer; publication looks at the
list.

## Lifecycle

```
draft ──→ submitted ──→ approved ──→ published
  │            │             │
  └────────────┴─────────────┴──────→ rejected
```

`draft` is the only state anything reaches on its own. Every other move goes
through `transition`, which:

- refuses an edge that is not in the table above;
- refuses an empty actor, with `a transition records the actor that asked for
  it`;
- re-runs the quality checks when the target is `published`, and refuses if
  anything fails, saving the refreshed report so the reviewer can see why;
- appends a `ProposalTransition` with the state, the time, the actor and the
  note.

A rejected proposal stays on disk with its history. A published one keeps the
Markdown as it was published, version and status rewritten.

## Publication

Publication writes into the catalog root named by `learning.publish_root` in
`configs/runtime.json`, resolved under the state directory —
`state/skills` by default, which is one of the default catalog roots.

The file lands at:

```
<publish root>/<type>/<name>/v<version>/skill.md
<publish root>/<type>/<name>/v<version>/policy.json
```

The version is one past the highest version of that id anywhere in the catalog,
so publishing a procedure whose id already exists supersedes it rather than
colliding with it, and `status` is rewritten to `published`. The registry picks
it up on the next call, because the set of files on disk changed and the
fingerprint with it. No restart, no reload command.

## Why there is no auto-approve

The runtime has one privilege that matters: it can change a system of record.
Everything in the governance layer exists to make that privilege narrow and
traceable. A procedure is what says which writes are reachable and under which
policy — so a code path that publishes a procedure without a person is a code
path that widens that privilege without a person.

It would also be circular. The material a draft is built from is a session the
runtime itself ran. Letting it approve its own output means the only check on
what it may do next is what it did last.

So: `transition` is the only function in the package that writes a state, it
refuses an empty actor, and `draft` never advances by itself. There is no flag,
no configuration key and no API parameter that changes this. The scenario suite
asserts it: scenario 15 requires that a transition with an empty actor is
refused and leaves the state untouched.

What is automated is everything up to the decision: the draft, the checks, the
publication mechanics. What a person does is read the file and say yes.

## Command line

```sh
opsdeck propose from-session <session_id> --by <actor>
opsdeck propose list [--state draft|submitted|approved|rejected|published]
opsdeck propose show <proposal_id>
opsdeck propose transition <proposal_id> --to <state> --by <actor> [--note "..."]
```

All four accept `--skills` to point at other catalog roots.

`from-session` prints the proposal id, its state, and one line per quality
issue. `--by` overwrites the actor on the drafting entry; without it the entry
records the session id. `list` prints id, state, source session and issue count.
`show` prints the whole proposal as JSON, Markdown and policy included.
`transition` prints the same line as `list` for the moved proposal; a refused
move prints the lifecycle's message on standard error and exits non-zero.

A worked sequence, against the example catalog:

```sh
opsdeck propose from-session walk --by s.reyes
opsdeck propose transition <id> --to submitted --by s.reyes --note "ran it once"
opsdeck propose transition <id> --to approved  --by s.reyes --note "wording read"
opsdeck propose transition <id> --to published --by s.reyes
opsdeck capabilities        # the new procedure is in the list
```

## HTTP

| Route | Does |
| --- | --- |
| `POST /proposals/from-session/{session_id}` | Draft from a session and store it. Returns the proposal |
| `GET /proposals` | Every proposal held |
| `GET /proposals/{proposal_id}` | One proposal |
| `POST /proposals/{proposal_id}/transition` | Move it. Body: `{"to": ..., "by": ..., "note": ...}` |

The drafting route takes no actor, so the drafting entry records the session id;
the transition route takes `by` in the body and it is required by the lifecycle,
not by the schema — an empty one comes back as a 409.

Errors are `{"error": ..., "detail": ...}`: 404 `unknown_proposal`, 409
`illegal_transition` for an edge that is not allowed, a missing actor or a failed
quality check, 422 `unknown_state` for a state that does not exist.

The service has no authentication and does not verify `by`. See
[governance.md](governance.md#identity).
