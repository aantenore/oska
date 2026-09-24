# Operating an installation

Five jobs that are nobody's during a turn: releasing a catalog, deciding on a
change the runtime proposed, reading what the deployment can say about itself,
removing the record of a conversation, and working out what to do when a system
stops answering. Each is a command, run by a person, outside the process that
answers people.

Every command below accepts `--skills` to point at other catalog roots, and
reads the same settings as the runtime. `oska config` prints what is in
force.

## Releasing a catalog

A directory of skill files is the right catalog for one process reading its own
disk, and the wrong one for several. Replicas share no disk and each would pick
a change up at its own moment. A revision closes that: validated once, copied
where nothing writes again, named after a hash of its contents.

```sh
oska revision build
```

It prints the revision, the tenant and how many skills it holds, followed by
any warning that was accepted at build time. A catalog carrying an error is not
released — the issues are printed and nothing is written — so what reaches the
store has already passed the validation the runtime would have run. Building
twice from the same files gives one identifier and one directory, because the
identifier comes from the content.

```sh
oska revision list
oska revision show 1f0a4c29b8de7a63
```

`list` prints every revision of the tenant, oldest build first, with a `*`
against the one this deployment serves. `show` prints the manifest: every skill
with the hash of the file that declared it, the validation report, the roots it
was built from and when.

```sh
oska revision use 1f0a4c29b8de7a63
```

`use` writes one line into `CURRENT`, beside the revisions under the state
directory. That file is what the runtime reads when `catalog.revision` in
`configs/runtime.json` is empty, so moving a deployment from one release to
another is an operation rather than an edit to a file under review. A
configuration that does name a revision wins, and `use` says so on standard
error rather than pretending it took effect.

What a running deployment does with a new revision is not a restart. A session
reads the whole catalog through the revision it was last served by, so a
procedure retired while somebody holds a pending approval does not vanish from
the turn that offered it. A turn that starts a new task adopts what the
deployment serves now and records the switch in the audit trail. Setting
`catalog.pin_sessions` to `false` lets every turn follow the deployment except
while an approval is waiting, which is held either way: the sentence a person
approved was composed from that revision.

With no revision built, the runtime reads the configured skill roots directly
and reloads when the files change. That is what the example catalogs do and
what one process on its own disk wants.

```sh
oska revision verify 1f0a4c29b8de7a63
```

`verify` runs the same check the runtime runs before it will serve a
revision — signature, and every file's hash against the signed manifest —
without serving anything, which is what makes it worth running by hand after a
revision is built or before `use` is pointed at one you did not just build
yourself. It answers only what `catalog.signing` decides: with no signer
configured, nothing is checked and it says so; with one configured, it fails
closed on a bad signature or a changed file whether or not signing is
`required`. See [configuration.md#catalog](configuration.md#catalog) and
[building-a-platform.md#7-sign-revisions](building-a-platform.md#7-sign-revisions).

## Deciding on a change request

Moving a proposal to `published` no longer writes a skill file. It composes a
change request under `changes.root`: the package exactly as it would be
published, the record of who asked and what the quality gate said, and a
summary to read without opening anything else.

```sh
oska change list
oska change list --state open
oska change show 8c20b659-10c1-4a04-ac36-defe0f734e7b
```

`show` prints the request and then the files its package holds. Read the skill
file and the policy beside it; a drafted procedure asks for a second approval on
every write it declares, with a per-session limit of one, until somebody edits
it. Run from the command line, these commands act as the operator at the
terminal, an identity that counts as verified on its own; reading or moving a
proposal or a change request over HTTP instead needs a principal carrying
`identity.reviewer_role`. See
[governance.md#who-may-do-what-over-http](governance.md#who-may-do-what-over-http).

```sh
oska change apply 8c20b659-10c1-4a04-ac36-defe0f734e7b \
  --into state/skills --by s.reyes
```

`--into` has no default and no configured fallback. This is the one place in
the system that writes into a catalog root, and the root is an argument because
nothing in the engine, the HTTP application or the improvement job knows which
root a deployment serves — none of them can call this.

Applying is not serving. The command says what it copied and then says that a
catalog root is a source: build a revision and point the runtime at it.

```sh
oska revision build
oska revision use <the revision that was printed>
```

A request that should not land is closed rather than left open:

```sh
oska change reject 8c20b659-10c1-4a04-ac36-defe0f734e7b \
  --by s.reyes --note "the rollback section is a placeholder"
```

`apply` is a copy, for a deployment that reviews a change request as a file on
the same disk. `publish` is the same request opened for review the way a team
already reviews everything else it ships:

```sh
oska change publish 8c20b659-10c1-4a04-ac36-defe0f734e7b \
  --repo /path/to/the/catalog's/git/checkout --path packs/ops/skills \
  --push --open-pr 'gh pr create --head {branch} --title {title} --fill'
```

It writes the package into an isolated git worktree, commits it on a new
branch (`change/<request id>`) built from `--base` (`HEAD` by default), and
removes the worktree — the operator's own checkout is never touched. Nothing
is pushed without `--push`, and no pull request is opened without a push
either: `--open-pr` names a command run with `{branch}` and `{title}`
substituted, so the actual pull-request tool is whatever the operator already
uses (`gh`, a hosted API wrapper, anything invoked from a shell), never
hard-coded into the runtime. The commit's author is the request's actor, and
`publish` refuses to make that commit when the actor was only stated and
never verified — a name given with no identity behind it — unless
`--allow-unverified-actor` says the operator means it anyway. A version that
already exists on the target branch is refused, the same guarantee `apply`
gives a catalog root.

`publish` does not decide the request — it stays `open` afterward, recording
its branch, whether it was pushed and what a pull-request command answered,
because the decision is still the review that happens on that branch. `apply`
and `reject` are the two moves that close a request, and either may still
follow a `publish`: a request merged through its pull request is typically
applied afterward too, so the change request record and the catalog root agree
about what landed. A change request is the record of one review, not a queue
entry to reuse. See [learning.md](learning.md#publication-composes-a-change-request).

## Reading the service-level report

```sh
oska slo report
oska slo report --since 7
oska slo report --json
```

Six numbers, all derived from records the deployment keeps anyway: the median
and the tail of how long a turn takes, how often a deterministic template spoke
because no model could, how often the read-back after a write did not confirm
it, how often a guardrail refused, and how often a request found no procedure at
all. The last is not an error rate — it is the signal that the catalog is
missing something.

The rule the report keeps is that a number nothing recorded is printed as not
computed, with the reason, and never as zero. A rate of zero because nothing was
measured reads exactly like a rate that is genuinely zero, and the two mean
opposite things. The two latency numbers come from traces, which are off by
default, so a deployment that has never switched tracing on is told to switch it
on rather than shown a zero.

One number is computed and still incomplete, and says so in a note: a reply the
model provider failed to write is not audited today, so a fallback at the reply
step is not counted.

`--audit` reads a trail other than the configured one, which is how a report is
run over a copy rather than over the file a running deployment is appending to.

## Forgetting a conversation

The account of one conversation is spread over eight stores — the receipts of
what it wrote, the audit trail of how it decided, the state it carried
between turns, the judgments recorded on its turns, what was learned about
how it reads, the trace spans that name it, the proposals drafted from it,
and the change requests composed from those proposals. A request to be
forgotten reaches all eight or it has reached none. An applied change request
is the one exception: its decision record — who asked, who decided, when,
which version, and where it landed — survives, with the drafted material
itself cleared.

```sh
oska forget session walk
```

That answers a person who asked. It runs whatever `retention.days` says and
whether or not the session is past it, and prints what it removed from each
store. Removal leaves one line behind: a retention event naming the session and
the counts, and nothing else — not what was said, not what was read, not what
was written. A deletion nobody can see having happened is indistinguishable from
a file somebody edited.

```sh
oska forget expired
oska forget expired --days 90
```

That applies the window, measured from when each session was last written.
`retention.days` is zero by default, which keeps everything: the command then
removes nothing and says so. Neither command runs inside the serving process. A
window is an operator's job on an operator's schedule.

Both are destructive and neither is reversible.

## When a connector is down

A connector classifies its own failures, and the difference matters more than
the message. A system that answered no — a record that is not there, a value it
would not take — is a system that is up, and the answer is terminal. A request
that was never well formed says nothing about the system at all. A call that
never arrived is retryable, and it is the only kind that counts.

After `resilience.failures_before_open` such calls in a row, that binding stops
being called: further calls are refused locally for `resilience.recovery_seconds`,
then one is let through, which either closes the breaker or opens it again. Only
that one binding degrades. A turn that reaches an unavailable capability reports
it as temporarily unavailable, names when it may be tried again, and carries on
with the operations that belong to other bindings.

Nothing is written when a capability is down. A write that never reached the
system is not a write: it settles nothing in the ledger, so the same intent may
be asked for again in the same session, and the receipt of the attempt stays in
the record because what was tried is part of the account.

What to do about it is a question for the system, not for the runtime. Read the
trail for the operation that failed, check the binding and the address it
names, and ask again once the system answers — the breaker finds out for
itself. `resilience.timeout_seconds` is what one call may take before it counts
as never having arrived; raising it makes a slow system look healthy and a turn
take longer.

Two files a session and its receipts live in are SQLite, and two turns of the
same process may write at once. `storage.busy_timeout_seconds` is how long one
waits for the other rather than failing the turn; ten seconds by default, and
worth raising only if turns start failing on a busy file rather than on the
system they were talking to.
