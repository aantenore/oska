# 13 Glossary

Each term has one meaning in this specification. Where a word is also used
loosely in the field, the narrow meaning below is the binding one.

**Actor** — the human identifier recorded on an approval or a proposal
transition. Supplied by the channel, recorded verbatim, never defaulted.

**Adapter** — a connector implementation for one class of system: a fixture, an
HTTP API, a command-line tool, a remote tool protocol.

**Approval** — a human decision recorded against a frozen payload hash. Scoped
to one session. Not a confirmation of a sentence; a confirmation of arguments.

**Audit event** — a typed record of one step of one turn, carrying a session
identifier, a turn identifier, a timestamp and a data object.

**Capability** — the set of operations a procedure may perform, declared in its
frontmatter and split into reads and writes. Not a description; a permission.

**Catalogue** — the collection of skill artefacts loaded from one or more
roots, versioned by directory, reloaded by fingerprint.

**Channel** — a transport that carries turns to the engine and renders results:
a command line, an HTTP endpoint, a chat adapter. Adds no behaviour.

**Composite** — a skill declaring an ordered sequence of procedures. Has
triggers and runtime wording, no policy of its own, and capabilities equal to
the union of its members'.

**Connector** — the executable side of an integration. Runs one declared
operation against one system and reports the result without judging it.

**Contract** — a typed model shared across layers, declared once and owned by
no layer.

**Declared scope** — the single sentence, derived from the published catalogue,
that names every action the system can perform. The only description of the
system's capability that exists.

**Discovery** — ranking published procedures against a request and choosing
one, or none.

**Entity type** — a declared record class: fields, an identifier field, an
optional state field with a closed set of states.

**Fast path** — the routing stage that matches a turn lexically against declared
triggers and configured utterances, above a threshold, when nothing is pending.

**Fingerprint** — a digest over the catalogue files, used to detect a change and
reload without a restart.

**Hydration** — resolving a procedure to its transitive closure: the procedure,
the entities it names, the integrations it reaches, and a composite's members.

**Idempotency fields** — the payload fields that form a write's ledger key. An
empty list means the whole payload keys the write.

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

**Operation** — a declared call: name, read or write flag, input schema, output
schema, entity binding, verification read for writes, idempotency fields.

**Outcome** — one observable result of a turn: a read, a receipt, a ledger hit,
a denial, a rejection, an invalid plan, a pending approval, a cancellation.

**Pending approval** — session state holding a frozen payload, its hash, the
plan it belongs to, the index it stopped at, and how many approvals are
required and given.

**Pending goal** — session state holding the procedures still to run, the step
reached, the inputs collected so far, and the questions still outstanding.

**Plan** — a typed, ordered list of operations for one procedure, plus the
inputs it still needs and the intent of the reply. Produced by cognition,
validated before anything runs, executed by interaction through governance.

**Policy** — an ordered set of rules governing the write operations of one
procedure. First matching rule decides; no match denies; no policy denies.

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

**Reply intent** — a short instruction from the plan telling the reply writer
what this turn was about. Does not add facts.

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

**Skill** — a versioned declarative artefact: typed frontmatter plus a body.
Four types: entity, integration, procedure, composite. Not a plugin, not code.

**Suggestion** — an action phrase derived from the catalogue for an entity a
read just returned. The only thing a reply may offer beyond its outcomes.

**Trigger** — an example utterance declared by a procedure or composite, used
by discovery and by the fast path. At least three per procedure, distinct.

**Turn** — one user message and everything the system does in response to it.

**Verification** — the read-back declared by an integration for a write,
compared against expectations. The only evidence that a write took effect.

**Verification status** — `verified`, `unverified` or `failed`. Only `verified`
permits a reply to claim success.

**Vocabulary** — the preferred term and synonyms for each entity type and
state, shared by the catalogue, the prompts and the replies.

**Write path** — the fixed sequence a write crosses: declaration, capability,
ledger lookup, policy, approval, recorded intent, execution, verification,
ledger update, audit.
