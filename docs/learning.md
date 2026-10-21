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
  note;
- writes the move only while the stored proposal is still in the state it was
  read in. Two reviewers, or two replicas, deciding the same proposal at once
  both pass the check against what they read; the store refuses the second
  write with `LifecycleError` (`409` over HTTP). Publication writes its move
  before it composes anything, so only the decision that won composes a change
  request; if composing then fails, the proposal goes back to `approved`.

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

### Publishing to a review a team already has

A directory under `changes.root` is a review for a deployment that keeps its
catalog on the same disk it runs the CLI from. A team that reviews everything
else as a pull request can ask for that instead:

```sh
oska change publish <request_id> --repo /path/to/checkout --path packs/ops/skills \
  --push --open-pr 'gh pr create --head {branch} --title {title} --fill'
```

`oska.learning.publishers.GitPublisher` writes the package into an isolated
git worktree, commits it on a new branch (`change/<request_id>`, from `--base`,
`HEAD` by default) as the request's actor, and removes the worktree — the
operator's checkout is untouched throughout. The branch and the commit are
recorded on the request as soon as they exist, so a push or a pull-request
command that fails afterward still leaves a request that says where its work
is. Nothing is pushed without `--push`, and `--open-pr` is refused without
`--push` too — a pull request is opened for a branch that exists somewhere to
open it against. `--open-pr` names a command run with `{branch}` and `{title}`
substituted; which tool that is (`gh`, a hosted API, an internal script) is the
operator's choice, never the runtime's. A version that already exists on the
target branch is refused, the same guarantee `apply` gives a catalog root.

The commit is authored as the request's actor, and `publish` refuses to make
that commit when the actor was only stated and never verified — a name given
over HTTP with no identity behind it, or a `--by` with none either. Passing
`--allow-unverified-actor` is the operator saying, at the moment of
publishing, that this one is trusted anyway. See
[governance.md#who-may-do-what-over-http](governance.md#who-may-do-what-over-http).

Publishing does not decide the request. It stays `open`, carrying where its
branch is, until a person applies it or rejects it — the same two moves as
always, made after the review that happens on the branch rather than in place
of it.

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
oska change publish <request_id> --repo <path> [--path <p>] [--base HEAD]
                     [--push] [--remote origin] [--open-pr <command>]
                     [--allow-unverified-actor]
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
| `POST /proposals/from-session/{session_id}` | Draft from a session and store it. Open to the session's own principal and to a reviewer. Returns the proposal |
| `GET /proposals` | Every proposal held. Reviewer role required |
| `GET /proposals/{proposal_id}` | One proposal. Reviewer role required |
| `POST /proposals/{proposal_id}/transition` | Move it. Body: `{"to": ..., "by": ..., "note": ...}`. Reviewer role required. Returns the proposal and, when the move composed one, its change request |
| `GET /changes` | Every change request held, oldest first. Reviewer role required |
| `GET /changes/{request_id}` | One change request. Reviewer role required |

There is no endpoint that applies a change request, and there is not going to be
one. The two `changes` routes read; nothing over HTTP writes into a catalog
root. The drafting route takes no actor at all — the drafting entry always
records the session id. The transition route takes `by` in the body, and only
reads it when the call carries no identity: a call with one is recorded under
the caller's principal whatever `by` says, and a call with neither an identity
nor a non-empty `by` is refused, `422 invalid_request`, before the lifecycle
ever runs.

Errors are `{"error": ..., "detail": ...}`: 404 `unknown_proposal`, 409
`illegal_transition` for an edge that is not allowed or a failed quality
check, 422 `unknown_state` for a state that does not exist, 422
`invalid_request` for a transition whose call names no actor at all. Every
route on this page also answers `401 identity_required` when the deployment
requires an identity and the call carries none; the drafting route also
answers `403 session_refused` when the caller is neither the session's own
principal nor a reviewer; the five reviewer-gated routes above also answer
`403 reviewer_required` to a principal without `identity.reviewer_role`. See
[governance.md#who-may-do-what-over-http](governance.md#who-may-do-what-over-http).
