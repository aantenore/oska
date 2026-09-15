# Example catalog: access management

Six accounts, five groups and the requests people raised to be put into them.
It exists to show the runtime running a domain it shares no words with: the
core knows about skills, operations, policies and receipts, and learns what an
account, a group and a membership are only by reading these files.

It is also the smaller of the two catalogs, and the one to copy when declaring
a domain of your own: three entities, three integrations, four procedures and a
composite, which is about the least that still exercises reads, a governed
write, a second approval and a sequence.

## What is in it

- `skills/` the catalog, one directory per skill and one directory per version.
- `fixtures/` the records the mock connector reads and writes: the directory of
  accounts and groups, the memberships that join the two, and four requests.
- `configs/connectors.json` binds each integration to the mock connector.

## The domain in one paragraph

An account belongs to groups; the permissions are on the group, so access is
granted and taken away by changing membership. A group is either standard or
privileged, and that single field is what every decision turns on: a standard
membership is granted on one approval, a privileged one on two. Every change is
supposed to have a request behind it, and the request is closed with what was
actually done, after the membership has been read back.

## How the skills map to the model

**Entities.** `entity.user` is identified by `username` and carries the account
status; `entity.group` by `name` and carries `sensitivity`; and
`entity.access_request` by `id`, with the state the request ended in. Nothing
routes to an entity: it is referenced by the procedures that touch it.

**Integrations.** `integration.directory` reads accounts, groups and who
belongs to what, and writes nothing. `integration.access_control` is the only
place membership changes, and both of its writes read back through
`membership.list` narrowed to the one account and the one group.
`integration.request_tracker` reads the requests and closes one of them.

**Runbooks.** `runbook.access_check` answers what a person has, read-only and
therefore with no policy. `runbook.access_grant` and `runbook.access_revoke`
each perform one write and carry a `policy.json`, and each carries an
`evaluation.json` naming the requests it must answer and the requests it must
not. `runbook.request_close` records the outcome on the request, which is a
write like any other and stops for its own approval.

`runbook.access_grant` also declares `principal_inputs: {username: subject}`.
The account it puts into a group is the account of whoever is signed in, taken
from the identity the channel authenticated and never asked for, so a session
nobody signed in to is refused rather than asked which account was meant.

**Composite.** `composite.request_fulfillment` runs the check, the grant and the
close in that order. Each step keeps its own policy, so the sequence stops
twice: once for the membership and once for the request.

## Two declarations worth reading

`membership.grant` and `membership.revoke` declare no `transitions`. A
membership is not a state of the account or of the group — the account stays
active, the group stays as sensitive as it was — so there is nothing honest to
promise about either. What the write leaves behind is read back explicitly:
`memberships.0.state` against the literal `active` or `revoked`.

`request.close` declares no transitions either, for the opposite reason: the
state it leaves the request in is not fixed but carried by the payload. The
expectation is written against `$payload.state`, which says exactly that the
request must read back in the state that was sent, and claims nothing about
which one it was.

## The mock binding

`configs/connectors.json` maps each declared operation onto a fixture, with the
keys documented in [`../ops/README.md`](../ops/README.md). Two of them carry
most of the weight here:

- `filter` narrows `membership.list` by both the account and the group, which
  is what makes the read-back about one membership rather than a list.
- `where` pins the record a write matches on with a second field taken from the
  payload (`"username": "$username"`), because a membership is identified by
  the pair and not by either half.

Writes land in an in-memory copy of the fixture, so a session changes state and
the files on disk stay as they are. The connector cannot create a record, so
`fixtures/memberships.json` carries a row for every account and group pair the
scenarios touch; a row in state `revoked` is a membership the account does not
currently hold.

## The scenarios

Eight sessions under `scenarios/`, run with `opsdeck conform --skills
examples/access/skills --scenarios examples/access/scenarios --connectors
examples/access/configs/connectors.json`. They reach conformance level 2: the
greeting answered from this catalog's own wording, a standard grant with its
verified receipt and a ledger replay, a privileged grant taking two approvals, a
revocation and the same revocation again, a fourth grant refused by the
per-session limit, the composite settling an open request end to end, a request
read while it is open being offered the move that completes it, and a grant
refused to a session nobody signed in to.
