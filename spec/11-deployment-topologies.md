# 11 Deployment topologies

The layers do not change between deployments. What changes is where the
connectors point, where the approval is collected, where the policies come from,
and what sits on the other side of the trust boundary. Three topologies are
described here. All three satisfy the same invariants; none of them relaxes the
write path. A fourth question cuts across them: how many processes serve the
catalog, and how many catalogs one deployment serves.

## Overlay on systems of record

The common case. The organization already has the systems: a ticketing
platform, a change-management tool, an orchestration platform, an inventory, a
CRM. The agent sits on top and owns none of them.

**Connectors** are adapters over those systems, one integration skill per
system. The operations are the subset the organization has decided an agent may
perform, which is almost never the full API. That subset is visible as a file
and reviewable as a file.

**State.** The agent keeps sessions, receipts, audit events and proposals. It
keeps no business data. A record it read this turn is context for this turn; the
system of record remains the only authority on it.

**Where the approval sits** is a deployment decision with three usual answers:

- *In the conversation.* The channel carrying the turn also carries the
  approval. Simplest, and adequate when the channel is authenticated and the
  writes are low-consequence.
- *In a ticket.* The approval request becomes an item in the tool the
  organization already uses for approvals; the decision returns as a turn. The
  payload hash travels with it, so what is approved is still exactly what was
  frozen.
- *In change management.* For writes under formal change control, the approval
  request is a change record, and the engine resumes when it is granted. The
  pending approval is session state, so waiting hours is the same to the engine
  as waiting seconds.

Whichever is chosen, the invariant is unchanged: the payload is frozen and
hashed before the request, re-checked on return, and the policy is re-evaluated
against current state before the write runs.

**Where policies come from** is likewise a deployment decision. The default is
the policy document next to the procedure, versioned with it. An organization
with a central policy service can put that service behind the same interface:
the engine asks for a decision on an operation, a payload and the entity records
it read, and receives an effect and a reason. The requirement that a missing
decision denies does not change, and the reason must still be printable to the
user, because a refusal the user cannot understand will be worked around.

## Standalone

The connectors are the system. Command adapters over local tooling, an HTTP
adapter over a service the same team runs, a fixture for development.

Nothing in the model changes. The write path is identical, and it is worth
saying why it is still worth having when there is no enterprise around it: the
approval gate is what makes a plan inspectable before it runs, the ledger is
what stops a retried request from acting twice, and the verifier is what makes
the difference between a command that exited zero and a change that actually
took effect. Those are not enterprise concerns; they are the reason the same
architecture is worth using for a single operator on a laptop.

The usual difference is the channel: a command-line session rather than a chat
adapter, one actor, one machine. Identity is still recorded, because the audit
trail is still the only account of what happened.

## Multi-agent

This section is informative. It describes how the model behaves when one
runtime reaches another, and it states no requirement of its own: everything
below follows from rules given elsewhere in this specification, applied to a
runtime whose connector happens to sit in front of another runtime.

Each agent is an OSKA runtime with its own catalog, its own governance and
its own ledger. There is no shared mutable state between agents beyond the
systems of record.

An agent reaches another agent the way it reaches anything else: through an
integration skill. The other runtime exposes a procedure; this runtime declares
an operation that invokes it, with an input schema, an output schema, an entity
binding and — if it writes — a verification read. From the calling side, another
agent is a system like any other. From the called side, the request arrives as a
turn and is routed, planned, governed and receipted normally.

Three consequences follow, and they are why this shape is the one the model
supports well:

1. **Approval does not delegate.** If the called procedure writes, its own
   approval gate applies, in its own session, under its own policy. An agent
   cannot approve on a human's behalf, and an operation that would require an
   approval nobody can give will stop and say so.
2. **The ledger does not span agents.** Idempotency is per session, per
   runtime. A caller that must not double-submit declares its own write with its
   own idempotency fields on the calling side.
3. **Capability is not transitive by accident.** A composite in one catalog
   may reference a procedure exposed by another runtime only because someone
   declared an operation for it. There is no discovery of another agent's
   capabilities at run time.

## More than one process

A catalog that is a directory beside the process is a property of one process,
not of a deployment. Two replicas reading their own disks are two catalogs
that agree most of the time; the times they disagree are exactly the times a
change is being rolled out. Nothing in the layers changes when a second replica
appears, but three things that were free stop being free.

**The catalog stops being a directory and becomes a release.** Each replica
MUST serve a revision, and the revision MUST be the same identifier on every
replica, because the identifier is derived from the content: if two replicas
report the same revision they are serving the same skills, and the question does
not have to be settled by comparing file timestamps. A revision is built once,
by whatever runs a release, and the replicas read it. A replica MUST NOT read a
mutable catalog directory while it serves.

**Rolling a change out becomes a decision with a time.** A directory-backed
runtime picks up a change the moment the fingerprint moves, which is a different
moment on every replica. A revision-backed runtime changes when it is told to:
the revision the configuration names, or the choice recorded beside the
revisions, and until then it serves what it served. That is what makes a
rollback an operation — serve the earlier identifier, which is still there —
rather than a restoration of files from somewhere.

**Session affinity stops mattering, and that is the point.** A session pins the
revision it started on, and the pin is in the session state, not in the process.
A turn that lands on another replica reads the same revision, because the
revision is an artifact both replicas can read and not a directory one of them
happens to have. Sessions, receipts and the audit trail are already shared
state; the catalog becomes shared state in the same sense.

What does not change: the ledger still keys per session, the approval still
belongs to the session that asked for it, and the policy is still re-evaluated
against current state before the write. A replica is not a trust boundary.

## More than one tenant

A deployment may serve more than one catalog — separate organizations,
separate estates of the same organization, a production catalog and a
rehearsal of the next one. The natural unit of that separation is the revision:
a revision belongs to exactly one tenant, its identifier is derived from the
tenant as well as the content, and a runtime resolves the revision of the tenant
it serves and nothing else. Nothing is shared between two tenants' catalogs,
because there is nothing between them to share: no directory both read and no
identifier both could resolve. A deployment serving several tenants is therefore
several resolutions of the same question — which revision of which tenant — and
not a new layer. What is not addressed here is everything below the catalog:
connector bindings, ledgers, session stores and audit trails are per deployment,
and a deployment that serves two tenants from one of those is putting two
organizations' records in one place. The catalog being separable does not make
the rest of it separable.

## Trust boundaries

| Party | Trusted for | Not trusted for |
| --- | --- | --- |
| Model provider | Nothing | Anything; every output is validated against declarations before it has effect |
| Connectors | Performing exactly their declared operations | Judging whether a write succeeded; that is the verifier's read-back |
| Systems of record | Being the authority on their own data | Being available, or returning what was expected |
| Channel | Authenticating the human and carrying the actor | Deciding policy or approving on the human's behalf |
| Humans | Approving writes and transitioning proposals | Nothing else is required of them |

The model provider is untrusted in the strong sense: a compromised or simply
poor provider can produce a plan with an undeclared operation, a payload that
fails a schema, a classification that claims an approval, or a reply that
overstates a result. Each of those is caught by a layer below cognition, and
each has a scenario in the conformance suite. The provider can waste a turn. It
cannot cause an effect.

Connectors are trusted narrowly. They run declared operations against systems
the organization chose, so they are inside the boundary in the sense that they
are allowed to act. They are outside it in the sense that their report of
success is not evidence: the read-back is. That distinction is what makes a
connector safe to write quickly and to bind by configuration.

Humans are the only source of approval. No configuration flag, service account
or upstream agent substitutes for one. A deployment that wants unattended
writes has to say so by removing the requirement, and a system that does that no
longer conforms.
