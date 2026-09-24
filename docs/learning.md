# Learning

A session that did something worth repeating can become a procedure. It becomes
one the same way a hand-written procedure does: as a file in a catalog root that
passes the validator, put there by a person. What the learning layer adds is a
drafting step, a lifecycle with a named actor on every move, and a change
request the runtime composes and cannot install.

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
written beside it, and runs four things:

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
4. **The evaluation set**, when there is a ranking to run it against and the
   skill being revised carries one. A proposal that cannot answer its own
   skill's declared cases is refused. A skill with no set is not refused:
   requiring one here would block every revision of every procedure written
   before evaluation sets existed, which pushes authors to paste in cases that
   pass rather than cases that matter.

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
- composes a change request when the target is `published`, and records its id
  in the transition note;
- appends a `ProposalTransition` with the state, the time, the actor and the
  note.

`transition` takes no catalog root and cannot be given one. `published` is the
end of what this function does: it means the package is composed and waiting
for a person, not that anything is being served.

A rejected proposal stays on disk with its history. A published one keeps the
Markdown as it was published, version and status rewritten.

## Publication composes a change request

Publication used to write a skill file under the running server, which made the
process that answers people the process that rewrote the catalog it answered
from. It now writes a directory under `changes.root`, resolved under the state
directory — `state/changes` by default:

```
<changes root>/<request id>/request.json
<changes root>/<request id>/summary.md
<changes root>/<request id>/<type>/<name>/v<version>/skill.md
<changes root>/<request id>/<type>/<name>/v<version>/policy.json
```

The package is laid out the way a catalog root is laid out, so applying a
request is a copy of one subtree rather than a translation. `request.json` holds
the proposal it came from, the skill id and version, who asked, when, the
quality report that let it through, and the state of the request.
`summary.md` is what a reviewer reads without opening anything else.

The version is one past the highest version of that id anywhere in the catalog,
so a request for a procedure whose id already exists supersedes it rather than
colliding with it, and `status` is rewritten to `published` in the file.

Where enrichment is on and a provider is given, the derived routing examples of
the new procedure are written beside the package inside the request, so they
travel with it and land in the catalog when a person applies it. That work
follows the publication and never gates it: a provider that fails is recorded
in the transition note and leaves the composed package exactly as it is.

A request is `open`, and a person makes it `applied` or `rejected`. Both are
final: a change request is the record of one review, not a queue entry to reuse.

```sh
oska change list
oska change show <request_id>
oska change apply <request_id> --into state/skills --by <actor>
oska change reject <request_id> --by <actor> --note "..."
```

`--into` has no default and no configured value it falls back to. This is the
one place in the system that writes into a catalog root, and the root is an
argument because nothing in the engine, the HTTP application or the improvement
job knows which root a deployment serves — none of them can call it.

Applying is not serving. A catalog root that has gained a version is a source,
and a deployment serving a built revision answers from the revision until a new
one is built and chosen. The command says so after it copies. A deployment
reading its skill roots directly picks the new version up on the next call,
because the set of files on disk changed and the fingerprint with it. See
[operating.md](operating.md).

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

The change request is the same argument made once more, at the last step. Even
a proposal a person has approved through every state of the lifecycle does not
become a file in a catalog root on the strength of that: composing the package
and installing it are separate acts, and only the second changes what is
served. The split is enforced by where the code lives rather than promised —
`transition` cannot take a catalog root, and the function that can is reachable
from a terminal and from nowhere in the serving process.

What is automated is everything up to the decision: the draft, the checks, the
package. What a person does is read the file and say yes.

## Where this sits on the autonomy ladder

The scheduled improvement job in `oska.improvement` describes how far the
system may improve itself without a person, in four levels, and this lifecycle
is where the top two of them meet. Level 2 is drafting and stopping: the job
turns a cluster of requests that reached nothing into a proposal in `draft`,
and that is the last thing it does with it. Level 3 is every move after
`draft`, and it has no code path at all — the improvement package imports
neither `transition` nor anything that writes a policy or a connector binding,
and a test walks the syntax tree of its modules to keep it that way.

So there are two ways a proposal comes into being, `draft_from_session` and the
job, and one way it moves: a person, through `transition`, with their name on
every step. See [improvement.md](improvement.md).

## Command line

```sh
oska propose from-session <session_id> --by <actor>
oska propose list [--state draft|submitted|approved|rejected|published]
oska propose show <proposal_id>
oska propose transition <proposal_id> --to <state> --by <actor> [--note "..."]

oska change list [--state open|applied|rejected]
oska change show <request_id>
oska change apply <request_id> --into <catalog root> --by <actor>
oska change reject <request_id> --by <actor> [--note "..."]
```

All of them accept `--skills` to point at other catalog roots.

`from-session` prints the proposal id, its state, and one line per quality
issue. `--by` overwrites the actor on the drafting entry; without it the entry
records the session id. `list` prints id, state, source session and issue count.
`show` prints the whole proposal as JSON, Markdown and policy included.
`transition` prints the same line as `list` for the moved proposal, and the
change request it composed when the move was to `published`; a refused move
prints the lifecycle's message on standard error and exits non-zero.
`change show` prints the request and then the files its package holds.

A worked sequence, against the example catalog:

```sh
oska propose from-session walk --by s.reyes
oska propose transition <id> --to submitted --by s.reyes --note "ran it once"
oska propose transition <id> --to approved  --by s.reyes --note "wording read"
oska propose transition <id> --to published --by s.reyes
oska change show <request id>              # read the package before it lands
oska change apply <request id> --into state/skills --by s.reyes
oska capabilities        # the new procedure is in the list
```

The last two are the part a person does. Up to `published` the runtime has
written a package and nothing else.

## HTTP

| Route | Does |
| --- | --- |
| `POST /proposals/from-session/{session_id}` | Draft from a session and store it. Returns the proposal |
| `GET /proposals` | Every proposal held |
| `GET /proposals/{proposal_id}` | One proposal |
| `POST /proposals/{proposal_id}/transition` | Move it. Body: `{"to": ..., "by": ..., "note": ...}`. Returns the proposal and, when the move composed one, its change request |
| `GET /changes` | Every change request held, oldest first |
| `GET /changes/{request_id}` | One change request |

There is no endpoint that applies a change request, and there is not going to be
one. The two `changes` routes read; nothing over HTTP writes into a catalog
root. The drafting route takes no actor, so the drafting entry records the
session id; the transition route takes `by` in the body and it is required by
the lifecycle, not by the schema — an empty one comes back as a 409.

Errors are `{"error": ..., "detail": ...}`: 404 `unknown_proposal`, 409
`illegal_transition` for an edge that is not allowed, a missing actor or a failed
quality check, 422 `unknown_state` for a state that does not exist.

The service has no authentication and does not verify `by`. See
[governance.md](governance.md#identity).
