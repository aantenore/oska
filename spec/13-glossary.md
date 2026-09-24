# 13 Glossary

Each term has one meaning in this specification. Where a word is also used
loosely in the field, the narrow meaning below is the binding one.

**Actor** — the human identifier recorded on an approval or a proposal
transition. Supplied by the channel, recorded verbatim, never defaulted, and
recorded with its actor source.

**Actor source** — where the actor of a proposal transition came from: the
principal the channel authenticated on the call, the operator at a terminal
running the command line, or a name the call only stated. A stated actor is
accepted only from a call with no identity where none is required, is marked
unverified on the proposal's history and on the change request, and is not
authored as a commit unless the operator allows it. See
[06-governance-layer.md](06-governance-layer.md).

**Adapter** — a connector implementation for one class of system: a fixture, an
HTTP API, a command-line tool, a remote tool protocol.

**Ambiguous failure** — a failed call whose request left and no usable answer
came back: a timeout, a connection reset after sending, a server error.
Retryable and ambiguous are the two ways a transport failure can happen; only
ambiguous forces a write to be settled by reading the record back rather than
left to a retry, and only the connector that made the call can tell them
apart. See [03-integration-layer.md](03-integration-layer.md).

**Approval** — a human decision recorded against a frozen payload hash. Scoped
to one session. Not a confirmation of a sentence; a confirmation of arguments.

**Audit event** — a typed record of one step of one turn, carrying a session
identifier, a turn identifier, a timestamp and a data object.

**Capability** — the set of operations a procedure may perform, declared in its
frontmatter and split into reads and writes. Not a description; a permission.

**Catalog** — the collection of skill artifacts loaded from one or more
roots, versioned by directory, reloaded by fingerprint.

**Change publisher** — what hands a change request to review: a directory, or
a version-controlled repository that receives it as one commit on a new
branch. Selected by name; never pushes unless asked, never forces a push, a
branch or a worktree, and never merges what it publishes. See
[09-evolution-layer.md](09-evolution-layer.md).

**Channel** — a transport that carries turns to the engine and renders results:
a command line, an HTTP endpoint, a chat adapter. Adds no behavior.

**Composite** — a skill declaring an ordered sequence of procedures. Has
triggers and runtime wording, no policy of its own, and capabilities equal to
the union of its members'.

**Connector** — the executable side of an integration. Runs one declared
operation against one system and reports the result without judging it.

**Contract** — a typed model shared across layers, declared once and owned by
no layer.

**Declared scope** — the single sentence, derived from the published catalog,
that names every action the system can perform. The only description of the
system's capability that exists.

**Discovery** — ranking published procedures against a request and choosing
one, or none.

**Domain** — a scope a person talks to on a platform: a set of packs, a
declared host identity, and the tenants it serves. A turn arrives in one
domain and nothing of another is visible to it; a skill of one domain is never
ranked, offered or executed in another. See
[14-platform-blueprint.md](14-platform-blueprint.md).

**Entity type** — a declared record class: fields, an identifier field, an
optional state field with a closed set of states.

**Environment grant** — the `environment` a platform manifest declares for a
domain or for a tenant: the variables, by name or by prefix, that the bindings
of the domain's packs, or the tenant's changes to them, may read with
`${NAME}`. A reference to any other variable is refused when the platform is
assembled; nothing declared means nothing may be read. See
[14-platform-blueprint.md](14-platform-blueprint.md).

**Fast path** — the routing stage that matches a turn lexically against declared
triggers and configured utterances, above a threshold, when nothing is pending.

**Fingerprint** — a digest over the catalog files, used to detect a change and
reload without a restart.

**Focus** — what a conversation is about, taken from what a governed read or a
verified write actually showed and from nothing else. One record shown is an
object in focus, several are a list in focus; a claim about it supplied by a
client is never taken as evidence and cannot override it. See
[07-cognition-layer.md](07-cognition-layer.md).

**Host identity** — who the runtime answers as in a domain: a name, a voice
and the languages it speaks. Declared in the platform manifest, never
inferred from the catalog, the conversation or the caller. See
[14-platform-blueprint.md](14-platform-blueprint.md).

**Hydration** — resolving a procedure to its transitive closure: the procedure,
the entities it names, the integrations it reaches, and a composite's members.

**Idempotency fields** — the payload fields that form a write's ledger key. An
empty list means the whole payload keys the write.

**Identity required** — `identity.required`: a channel refuses with 401 every
call that carries no identity, except the read that says whether the runtime is
up, and the read of what it measures where the configuration marks it public.
On by default in token mode. A deployment that requires none answers a call
with none as a conversation, and serves it as its single user, review
included, only where it declares itself single-user. See
[06-governance-layer.md](06-governance-layer.md).

**Integration** — a skill declaring a closed set of operations against one
system.

**Invalid plan** — a plan naming an undeclared operation, a write outside the
procedure's capabilities, or a payload failing an input schema. An outcome, not
an exception.

**Layer** — one of the eight tiers of the model. A layer depends only on the
layers below it and on contracts.

**Ledger** — the durable record of write intents and their outcomes. Also the
retry mechanism: a key already recorded in a session is not written again.

**Ledger hit** — the outcome when a write's key is already recorded for the
session. The original receipt is returned; no connector is called.

**Model provider** — any component answering a structured completion or
embedding request. No vendor is named in this specification.

**Namespace (storage)** — the key, derived from a (tenant, domain) pair, that
keeps one pair's sessions, receipts and write claims apart from every other
pair's in a shared database. A query not restricted to its namespace is
refused by the store itself, not left to its caller. See
[06-governance-layer.md](06-governance-layer.md).

**Operation** — a declared call: name, read or write flag, input schema, output
schema, entity binding, verification read for writes, idempotency fields.

**Outcome** — one observable result of a turn: a read, a receipt, a ledger hit,
a denial, a rejection, an invalid plan, a pending approval, a cancellation.

**Pack** — a versioned, self-contained directory of declarations for one
business area: its entities, integrations, procedures and composites, the
policies beside its procedures, its connector bindings, and the scenarios and
evaluation sets that measure it. The unit a team writes, reviews, versions and
ships. See [14-platform-blueprint.md](14-platform-blueprint.md).

**Pack manifest** — `pack.yaml`: a pack's name and version, the runtime
versions it requires, the packs it depends on, what it provides, an optional
default host, its synonyms, and the conformance level it claims. Checked
against what the pack's skills actually declare. See
[14-platform-blueprint.md](14-platform-blueprint.md).

**Pending approval** — session state holding a frozen payload, its hash, the
plan it belongs to, the index it stopped at, and how many approvals are
required and given.

**Pending goal** — session state holding the procedures still to run, the step
reached, the inputs collected so far, and the questions still outstanding.

**Plan** — a typed, ordered list of operations for one procedure, plus the
inputs it still needs and the intent of the reply. Produced by cognition,
validated before anything runs, executed by interaction through governance.

**Platform** — several packs grouped into domains and served for tenants from
one runtime process. Built by writing a manifest, not code: the runtime is the
same for every platform, what changes is the packs it serves. See
[14-platform-blueprint.md](14-platform-blueprint.md).

**Platform manifest** — `platform.yaml`: what an organization deploys. Names
the platform, maps each pack to a path and an accepted version range, declares
the domains, and may name a default domain, its channels, its storage backend
and the conformance level every domain must attain. See
[14-platform-blueprint.md](14-platform-blueprint.md).

**Policy** — an ordered set of rules governing the write operations of one
procedure. First matching rule decides; no match denies; no policy denies.

**Presentation directive** — what a turn asks a presentation surface to show:
a procedure, a moment and a view, with fields filled only from evidence the
runtime holds. A binding the evidence cannot answer drops the whole directive;
a directive never writes anything, and nothing a channel does with it changes
what the turn did. See [08-interaction-layer.md](08-interaction-layer.md).

**Presentation moment** — the point in a turn a procedure's directive is bound
at, taken in a fixed order: a verified write, a resolved selection, several
results, one result, or the goal's completion. A domain that does not enable
presentation returns none, whatever a procedure declares. See
[08-interaction-layer.md](08-interaction-layer.md).

**Procedure** — a skill describing something a person asks for: triggers,
entities, integrations, capabilities, runtime wording, a policy when it writes,
and a body written for the person who owns it. Called a runbook in the
reference implementation.

**Proposal** — a candidate skill, its quality report and its transition
history, moving through draft, submitted, approved, published or rejected.

**Quality gate** — the lints and validation a proposal must pass to be worth
reviewing.

**Receipt** — the record of one write attempt: the payload sent, the response,
the status, the verification result and its evidence.

**Refinement** — a field a comparative may narrow a list in focus on: one the
entity declares comparable, on which the shown records actually differ, and
which the read that showed them accepts as a filter. Only refinements the
turn's own evidence supports reach the planner. See
[07-cognition-layer.md](07-cognition-layer.md).

**Reply intent** — a short instruction from the plan telling the reply writer
what this turn was about. Does not add facts.

**Reviewer role** — the role, named by `identity.reviewer_role`, a principal
carries in its `roles` attribute to read proposals and change requests and to
move a proposal. A principal without it is refused with a reason naming only
that role; drafting a proposal from a session stays open to the session's own
principal and to a reviewer. See [06-governance-layer.md](06-governance-layer.md).

**Router** — the component deciding what kind of turn this is, in three stages:
rules, fast path, model.

**Runtime wording** — the `action`, `action_on_selection` and `order` a
procedure declares, used when the system names it to a person.

**Scenario** — a recorded conversation with per-turn expectations and a scripted
provider, executed as a conformance test.

**Scripted provider** — a model provider returning pre-recorded responses in
order, asserting what the prompt reaching each step contained. Makes the
conformance suite deterministic and offline.

**Session** — one conversation, with its own state, its own approvals and its
own ledger scope.

**Session token** — a signed, three-part bearer credential
(`header.claims.signature`) a channel may verify itself: `HS256` only, a
constant-time signature check before any claim is read, its required claims
present, and its `sid` bound to the session of the call it acts in. See
[06-governance-layer.md](06-governance-layer.md).

**Signed revision** — a built catalog revision whose manifest is signed by a
named signer, verified on every load whether or not signing is required, and
refused when a file's hash or the signature itself does not check out. See
[05-skill-layer.md](05-skill-layer.md).

**Single-user deployment** — `identity.single_user`: a deployment that declares
whoever reaches it without an identity to be its one user, reviewer included.
Refused in token mode. Without it, a call with no identity is never worth more
than an identity without the reviewer role. See
[06-governance-layer.md](06-governance-layer.md).

**Skill** — a versioned declarative artifact: typed frontmatter plus a body.
Four types: entity, integration, procedure, composite. Not a plugin, not code.

**Subject** — the entity a procedure declares it acts on, and whether it joins
the standing focus. A subject that joins the focus receives the resolved
identifier as a collected input, stated apart from what the person typed. See
[04-ontology-layer.md](04-ontology-layer.md).

**Suggestion** — an action phrase derived from the catalog for an entity a
read just returned. The only thing a reply may offer beyond its outcomes.

**Tenant** — an organization, or an estate of one, served by a domain. A
dimension of resolution: the same domain resolves to different state, and may
resolve to different bindings, for different tenants. See
[14-platform-blueprint.md](14-platform-blueprint.md).

**Tenant overlay** — a tenant's own directory beside the platform manifest,
holding a `connectors.json` that may change only the `base_url` and `headers`
of an HTTP binding of the same integration id, and a `runtime.json` fragment
limited to what stays inside the tenant: reply wording, discovery languages and
resilience, whose costs a tenant may lower and never raise. See
[14-platform-blueprint.md](14-platform-blueprint.md).

**Trigger** — an example utterance declared by a procedure or composite, used
by discovery and by the fast path. At least three per procedure, distinct.

**Turn** — one user message and everything the system does in response to it.

**Verification** — the read-back declared by an integration for a write,
compared against expectations. The only evidence that a write took effect.

**Verification status** — `verified`, `unverified` or `failed`. Only `verified`
permits a reply to claim success.

**Vocabulary** — the preferred term and synonyms for each entity type and
state, shared by the catalog, the prompts and the replies.

**Write claim** — the atomic step that takes the intent of a write: the check
that nothing already settles its ledger key and the record of its receipt
happen as one step for every thread and process sharing the ledger. A caller
that loses the claim reports the receipt that holds it instead of sending the
write again. See [06-governance-layer.md](06-governance-layer.md).

**Write path** — the fixed sequence a write crosses: declaration, capability,
ledger lookup, policy, approval, recorded intent, execution, verification,
ledger update, audit.
