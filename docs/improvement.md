# Self-improvement

A system that learns from its own traffic has to say, once and in public, where
it stops. This is that statement, and `opsdeck improve` is what it looks like
from a terminal. The ladder has four levels. The top one has no code path, and
that is the part worth reading.

## The ladder

**Level 0 — observe.** Signals are read back from the audit trail and the write
ledger, and the judgments people recorded against answers are read with them.
Nothing is changed.

**Level 1 — adjust what is cheap and reversible, and show every change.**
Learned phrasings are written as separate files under the improvement root, one
per procedure, each undone by deleting it. A preference about how one session's
answers read is written the same way. Threshold tuning is proposed as an
overrides file a caller may apply. Nothing authored is edited.

**Level 2 — draft, and stop.** A request that reached no procedure becomes a
gap, and a gap becomes a proposal in `draft` with the entities and operations
the ontology already declares. A draft is a sentence addressed to a reviewer,
not a capability.

**Level 3 — never without a person.** Moving a proposal past `draft`,
publishing anything that can write, changing a policy, changing a connector
binding: none of it happens here, under any configuration, on any code path.

## Running the job

```sh
opsdeck improve run                     # the five steps, then the report
opsdeck improve run --apply-tuning      # also write the tuned thresholds
opsdeck improve run --cases <path>      # evaluate against another labeled set
opsdeck improve show                    # the report of the last run
opsdeck improve show --json
opsdeck improve evaluate                # route the labeled set, change nothing
opsdeck improve feedback <session_id> --turn 2 --score down --comment "..."
opsdeck improve revert <target>         # delete what was learned, or what was recorded
```

All of them take `--skills` to point at other catalog roots. `run`, `show` and
`evaluate` take `--json`.

It is a scheduled job, not a turn. Run it nightly, or after a week of traffic,
or by hand when routing starts feeling wrong. It needs a state directory with
an audit trail in it; with no provider configured it does its work by keyword
overlap instead of embeddings, which is the same choice the runtime makes.

## What it reads

Almost nothing is recorded to make this job work. It reads two files that are
written for other reasons, and one that is written for this one.

The **audit trail** says what was asked, where the request was routed, what
score discovery gave it and which operations ran. The **write ledger** says
whether a write was confirmed by its read-back. One turn of the trail becomes
one signal: the text, the language, the procedure reached, the score, whether
it was a tie, and what completed.

A signal is *grounded* when it names a procedure and something actually
finished — a verified write, or reads that completed. That pair, the words a
person used next to the procedure the request turned out to mean, is free
ground truth for routing, and it is the whole of what the job learns from.

The third is the **judgments**, described below.

## A judgment on an answer

What a turn leaves behind says what was asked, where it went, what ran and
whether a write was confirmed. None of it says whether the answer was any good.
A person knows, and until they are asked the system never finds out.

```sh
opsdeck improve feedback walk --turn 2 --score down --comment "too long, and I asked in Italian"
```

That is the whole of it: `up` or `down` against one turn of one session, with an
optional comment. A turn is named by its position in the session, counted from
one, because that is the number a person can see — the identifier the audit
trail gives a turn is random and nobody could quote it back.

The command writes one line to a file under the state directory and does nothing
else. No procedure changes, no threshold moves, no wording is learned because of
it. The judgment is inert until the scheduled job reads it, and the job is the
only thing that reads it for anything beyond reporting. The report counts the
judgments and breaks them down by score.

## A preference about how an answer reads

A `down` sometimes carries a comment, and a comment is sometimes not about the
answer but about the shape of it: too long, no lists, greet me first. That is
worth keeping, on a very short leash.

The leash is one sentence. **A preference may change how an answer reads. It may
never change what the runtime does.** It reaches the reply writer as a fact and
reaches nothing else: not the planner, not a policy, not a payload, not a
routing decision. A comment asking for something to be *done* is not a
preference about reading, and is discarded rather than stored, because a stored
one would be a standing instruction nobody approved.

Classifying a comment happens at job time and never during a turn. A turn that
had to decide what a comment meant would be spending a model call on something
nobody asked it for, and would be acting on a sentence before anyone had read
it.

A preference is a file under the state directory, one per session, like a
learned phrasing. Deleting the file is the whole of the undo.

## What it writes

Five things, all under the state directory, none of them in a catalog root.

**Learned phrasings**, at `improvement.learned_root`, one directory per
procedure holding one file. Three filters stand between a request and a kept
phrase: it has to come from a grounded turn, it has to say something the
declared triggers and the phrases already kept do not already say, and anything
that looks like an identifier, a reference or an address is replaced first,
using patterns read from a file rather than written into the code. The index
reads these files when it ranks the catalog; it never writes them, so a turn
costs nothing extra for them being there.

**Preferences**, at `state/preferences`, one file per session, holding what the
comments on that session's judgments said about how its answers should read.
The reply writer reads them and nothing else does.

**An overrides file**, `discovery-overrides.json`, written only with
`--apply-tuning`. It holds the thresholds the search found, the ones they would
replace, and what the search refused. Nothing writes into `configs/runtime.json`:
a tuned number that overwrites an authored one leaves nothing to compare
against.

**Proposals**, in the proposal store, each in `draft`. One per gap.

**The report**, at `improvement.report_path`, rewritten every run.

Authored files are not on that list. That is the point of the list.

## How to revert

By deleting, in every case.

```sh
opsdeck improve revert runbook.service_restart   # what was learned for a procedure
opsdeck improve revert walk                      # what was recorded about a session
```

One argument, two things it may name, because both are files this job wrote and
both are undone by deleting them. Naming a procedure deletes its learned file
and, with it, every effect it had on routing: the index folds those files into
the fingerprint it holds, so the next ranking is built without them and is the
ranking that was there before they were learned. Naming a session deletes its
preferences, which stops them reaching the reply writer of that session — the
only place a preference ever reached. Nothing has to be restarted. A target that
names neither leaves a non-zero status and the message that nothing was learned
or recorded for it.

Two things `revert` does not cover, because neither was ever in force. The
overrides file is reverted by deleting it, and it did nothing unless a caller
applied it. A drafted proposal is reverted by rejecting it, or by leaving it
where it is: a draft does nothing until a person moves it. And the judgments
themselves are not reverted at all — a judgment is a record of what somebody
thought, not a change to undo.

There is nothing else, because nothing else was changed: an edit to an authored
skill cannot be undone by deleting anything, which is why the job never makes
one.

## The evaluation set as an alarm

`examples/ops/evaluation/routing.json` is sixteen labeled requests, written by
hand in two languages, in wording that is not a copy of the declared triggers.
Two of them expect a tie, on purpose: a system that resolves a genuine
ambiguity silently is wrong in a way no accuracy number would show.

The report carries three numbers. **Precision** is how often an answer was
right, over the cases that were answered at all — a case the system declined is
an abstention, not a mistake. **Decisive rate** is how often it committed to a
single procedure. **Model pick rate** is how often it reported a tie and would
hand the choice to a model. The failures are listed one per line, with what was
expected and what was routed.

The set exists to raise an alarm, never to trigger a change. A run whose
precision is below the previous report's exits non-zero and says so on standard
error; nothing is undone, nothing is rolled back, and the report names the
cases that were missed so a person can decide. A drop usually means something
moved — a new procedure, an edited trigger, a different provider — and that is
a thing to look at, not a thing to correct automatically.

`opsdeck improve evaluate` runs the same routing and writes nothing at all,
which is the right command to run against a change before committing it.

Tuning uses the same set and is bounded by `improvement.bounds`. A search
allowed to go anywhere will discover that a floor of zero and a margin of zero
answer every case decisively, and will report a higher decisive rate for a
system that has stopped declining. A better value outside the bounds is
refused, and the refusal is listed in the report rather than dropped.

## Why level 3 has no code path

The runtime has one privilege that matters: it can change a system of record. A
procedure is what says which writes are reachable and under which policy. So a
code path that moves a proposal past `draft` is a code path that widens that
privilege without a person, and it would be circular besides — the material a
draft is built from is traffic this runtime routed itself, so letting it
approve its own output makes the only check on what it may do next be what it
did last.

The boundary is the deliverable, and it is enforced rather than promised. The
package imports neither the proposal lifecycle nor anything that writes a
policy or a connector binding. The job checks, at run time and not only in a
test, that every proposal it produced is in `draft` and refuses to report one
that is not. And a test parses every module of the package and walks its syntax
tree, failing if any of them reaches `submitted`, `approved` or `published`.

What is automated is everything up to the decision. What a person does is read
the draft and say yes, through the lifecycle described in
[learning.md](learning.md).
