# 05 Skill layer (L3)

## Purpose

L3 holds what the system knows how to do, as files. A skill is a versioned
declarative artifact: typed frontmatter that machines read, and a body written
for the person who owns the procedure. The runtime is generic; the catalog
makes it specific. Adding a capability is adding a file and publishing it.

This layer also computes what the system says about itself. The greeting, the
refusal, the list of next actions after a read: all derived from the published
catalog, never written by hand.

## Modules

### Skill artifact

Responsibility: carry one declaration. Four types, one artifact format.

- **entity** — a record class (L2). Declares fields. No triggers, no
  operations, no policy.
- **integration** — a closed set of operations (L1). Declares operations. No
  triggers.
- **procedure** — a runbook: something a person asks for. Declares triggers,
  the entities it works on, the integrations it reaches, the operations it may
  use split into reads and writes, the wording the runtime uses to name it, and
  a policy when it writes.
- **composite** — an ordered sequence of procedures. Declares triggers and
  member procedures. No policy of its own.

```text
SkillFrontmatter:
  id: string                # "<type>.<name>", lowercase
  type: entity|integration|procedure|composite
  version: integer >= 1
  status: draft|published|retired
  description: string       # 20..200 characters
  triggers: [string]        # >= 3 for procedure and composite; empty otherwise
  entities: [string]
  integrations: [string]
  procedures: [string]      # composite only, ordered
  capabilities: {reads: [string], writes: [string]}
  runtime: {action, action_on_selection, order}   # procedure and composite
  operations: [OperationSpec]   # integration only
  fields: [EntityField]         # entity only
  human_inputs: [string]        # procedure and composite; payload fields only a person supplies
  policy: string | null         # path to the policy document

SkillBody:
  purpose, preconditions, steps, verification, rollback, extra
```

The body is prose for a human. The frontmatter is the contract. Nothing in the
body grants a capability.

#### Human-only inputs

`human_inputs` names payload fields a procedure reserves to the person. They
are not fields the model may derive, infer from context, reword from an earlier
turn, or supply a sensible default for: a duration somebody has to decide, a
sentence that goes on a permanent record, a reason an auditor will read back.
The model's judgment is good enough for most of a payload and is the wrong
authority for these.

The rule is enforced where a plan is checked, not asked for in prose. A plan
whose operation payload carries a human input is rejected unless the value is
exactly what the person gave when they were asked for it; the field belongs in
the plan's `missing` list instead, with the question to put to them. The
planning prompt states the rule and lists the fields, so the usual case is a
plan that asks rather than a plan that is repaired — but the prompt is the
courtesy and the check is the contract.

A reserved field is unlike a capability in one respect worth stating: it does
not restrict what may eventually be sent. It restricts where the value may come
from. Once the person has answered, the write proceeds like any other.

#### Evaluation set

A skill may carry an evaluation set beside it, in the version directory that
holds its file. Each case is a request and a verdict — that request must reach
this skill, or it must not — and may name the language the request is written
in.

```text
EvaluationCase:
  text: string                 # the request, as a person would write it
  expects: this | not this     # must reach this skill, or must not
  language: string | null      # what the request is written in
```

The set is the worked example that a ranking threshold cannot be. A threshold is
a guess about one catalog at one moment; the words a person would really use,
written beside the procedure they meant, stay true as the catalog grows. The
set travels with the skill through review and publication, it is run whenever
the catalog changes, and a case that fails is a build failure rather than an
alarm raised somewhere else.

The two verdicts are not held to the same bar. A case that must reach its own
skill passes when that skill is the top of the ranking and the ranking is
decisive enough for the runtime to choose it, because a request that ranks first
below the floor reaches nothing in a conversation either. A case that must not
reach it passes only when that skill is not the top of the ranking at all: a
near-miss that ranks first is one changed threshold away from taking the
request. Running a set ranks the way the runtime ranks; an evaluation that
scored requests its own way would be measuring itself.

`spec/12-conformance.md` says what the sets are for across a catalog and how
the cases worth writing are chosen.

### Catalog

Responsibility: load every skill from one or more roots, keep them addressable
by identifier, and reload when the files change.

```text
Catalog:
  skills() -> [Skill]
  published() -> [Skill]
  by_type(type) -> [Skill]
  get(id) -> Skill | null        # highest published version
  closure(id) -> Hydrated
  fingerprint() -> string
```

Versioning is by directory: one directory per skill, one per version. `get`
returns the highest published version. Draft and retired versions are loaded
so the validator can see them, and are not returned by `published`.

### Revision

Responsibility: turn the roots into an artifact that can be served, named by
what it contains.

A directory beside the process is the right catalog for one process and the
wrong one for several. Replicas have no shared disk, modification times differ,
and each replica would pick up an edit at its own moment; a reader cannot say
which catalog answered a question, because the catalog has no name. A
release closes all of that. Building one reads the roots, validates them, copies
what held up into a directory that is never written again, and writes a manifest
beside it.

```text
Manifest:
  revision: string           # derived from the manifest itself
  tenant: string
  created_at: timestamp
  skills: { "<id>@v<version>": content_hash }
  report: [string]           # the validation report that let it through
  source_roots: [string]

RevisionStore:
  build(roots, tenant) -> Manifest
  list(tenant) -> [revision]        # oldest build first
  latest(tenant) -> revision | null
  get(tenant, revision) -> Manifest | null
  path(tenant, revision) -> location
```

The identifier is a hash of the tenant and the skill hashes, and of nothing
else. When and where a revision was built, and out of which roots, describe the
build and not the catalog, so two replicas building the same skills agree on
the name without speaking to each other, and building the same skills twice
produces one identifier and one directory rather than two. A build that finds
its identifier already in the store writes nothing and returns what is there.

A revision that carries a validation error is never written. What was already
required of a served catalog — that it holds up before anything reads it — is
moved earlier, to the moment of release, where it stops a bad catalog from
reaching any replica rather than stopping each replica in turn.

A catalog served as a revision does not reload. Its identifier is its
fingerprint: the files under it cannot move, so the caches that key on the
fingerprint — the index, the ontology graph, the fast path — are correct without
re-reading anything. Serving a directory remains legal, and remains what a
single process on its own disk should do.

### Validator

Responsibility: decide whether a catalog is coherent, and report every
problem rather than the first.

It checks the frontmatter against a machine-readable schema, then the things a
schema cannot express: referenced entities, integrations and member procedures
exist and are published; every identifier in `capabilities.writes` is an
operation with `write` true in one of the listed integrations; every write
operation has a verification read of the same integration; a procedure or
composite has runtime wording and at least three triggers; a procedure with
writes declares a policy; a composite declares none; a composite's capabilities
equal the union of its members'; the policy document validates against the
policy schema and names its own procedure.

```text
Validator:
  validate(skill, all_skills) -> [ValidationIssue]
  validate_all(skills) -> [ValidationIssue]

ValidationIssue: {skill_id, code, message, path}
```

### Capability Resolver

Responsibility: turn the published catalog into the words the system uses
about itself.

```text
CapabilityResolver:
  available_processes() -> [Skill]            # published procedures and composites, ordered
  actions_for_entity(entity_id) -> [Skill]    # published procedures with a write on that entity
  action_phrase(skill, on_selection) -> string
  phrase_list(skills) -> string               # "a, b and c"
  declared_scope() -> string                  # one sentence naming every action
```

`declared_scope` is the only description of the system's capability that exists.
It is used in the greeting, in refusals, and in the prompts the cognition layer
builds.

### Hydration

Responsibility: resolve a procedure to everything it needs and nothing else.

```text
Hydrated:
  procedure: Skill
  entities: [Skill]
  integrations: [Skill]
  procedures: [Skill]    # a composite's members
```

The closure is the unit handed to L5. A planner sees the procedure body, the
entity fields and the exact operations it may emit. It does not see the rest of
the catalog.

## Invariants

- A procedure with a non-empty `capabilities.writes` declares a policy.
- A composite declares no policy; each member runs under its own.
- A composite's capabilities equal the union of its members' capabilities.
- Triggers exist only on procedures and composites.
- The highest published version wins; a draft never routes.
- The catalog reloads on a change of file fingerprint without a restart. A
  catalog served as a revision does not change at all: its identifier is its
  fingerprint.
- The same skill files give the same revision identifier, wherever and whenever
  they are built.
- The resolver names only published skills. A retired procedure disappears from
  what the system offers in the same reload.
- A skill body cannot widen a capability. Only frontmatter grants anything.
- A field a procedure reserves to a person is either absent from the plan and
  listed among what is still missing, or carries exactly the value that person
  gave. There is no third case.

## Requirements

- **OSKA-SKL-1** — Every skill MUST declare an identifier, a type, an integer
  version, a status and a description, and the identifier MUST match its type.
- **OSKA-SKL-2** — A procedure or composite MUST declare at least three distinct
  triggers and runtime wording; an entity or integration MUST declare none.
- **OSKA-SKL-3** — Every identifier in `capabilities.writes` MUST be an
  operation declared with `write` true by one of the skill's listed
  integrations; every identifier in `capabilities.reads` MUST be a declared
  read operation of one of them.
- **OSKA-SKL-4** — A procedure with a non-empty `capabilities.writes` MUST
  declare a policy document, and that document MUST name the same procedure.
- **OSKA-SKL-5** — A composite MUST NOT declare a policy, and its capabilities
  MUST equal the union of its members' capabilities.
- **OSKA-SKL-6** — The catalog MUST resolve an identifier to the highest
  published version, and MUST NOT return draft or retired versions from
  `published`.
- **OSKA-SKL-7** — The catalog MUST detect a change to its roots by
  fingerprint and reload without a restart.
- **OSKA-SKL-8** — The validator MUST report every issue it finds, not only the
  first, and each issue MUST identify the skill and the offending path.
- **OSKA-SKL-9** — A catalog with any validation issue MUST NOT be served to
  the cognition layer.
- **OSKA-SKL-10** — Hydration MUST return the transitive closure of a procedure
  and MUST NOT include skills outside it.
- **OSKA-SKL-11** — Every sentence the system states about its own capability
  MUST be derived from the published catalog; no capability copy may be
  hard-coded in code, prompt or template.
- **OSKA-SKL-12** — `actions_for_entity` MUST return published procedures that
  hold at least one write on that entity, and MUST exclude read-only procedures
  and composites.
- **OSKA-SKL-13** — A skill file that cannot be parsed MUST be reported as a
  validation issue and MUST NOT be silently skipped.
- **OSKA-SKL-14** — The skill body MUST NOT be able to grant, widen or
  otherwise affect any capability; only frontmatter is binding.
- **OSKA-SKL-15** — A procedure or composite MAY declare `human_inputs`, naming
  payload fields that only the person the runtime is speaking to may supply. An
  entity or an integration MUST NOT declare them.
- **OSKA-SKL-16** — Plan validation MUST reject a plan whose operation payload
  carries a field named by `human_inputs` of the procedure being planned or of
  any procedure in its closure, unless the value is exactly the one collected
  from the person. The field MUST instead appear in the plan's `missing` list
  with the question to ask.
- **OSKA-SKL-17** — The planning prompt MUST state that human inputs are never
  filled in by the model and MUST list the fields the procedure reserves. The
  prompt MUST NOT be the only enforcement.
- **OSKA-SKL-18** — A catalog MAY be served as a revision: an immutable
  artifact holding the skill files of one tenant and a manifest naming every
  skill with the hash of its content, the validation report that admitted it,
  and the roots it was built from.
- **OSKA-SKL-19** — A revision identifier MUST be derived from the manifest
  content alone — the tenant and the skill hashes — so that the same skill files
  MUST yield the same identifier however often and wherever they are built. The
  build time, the build host and the source roots MUST NOT affect it.
- **OSKA-SKL-20** — Building a revision MUST validate the roots, and MUST NOT
  write anything when any issue is an error. The manifest of a revision that is
  written MUST carry the report of the issues that were accepted.
- **OSKA-SKL-21** — A revision MUST NOT be written twice. A build whose
  identifier the store already holds MUST leave that revision untouched and
  return it. A revision that has been written MUST NOT be modified afterwards.
- **OSKA-SKL-22** — A partially written revision MUST NOT be readable as one.
- **OSKA-SKL-23** — A catalog served as a revision MUST report the revision
  identifier as its fingerprint, and MUST NOT re-read its files to decide
  whether it has changed.
- **OSKA-SKL-24** — A runtime serving a revision MUST NOT read a mutable
  catalog directory while it serves.
- **OSKA-SKL-25** — A skill MAY carry an evaluation set beside it, in the
  version directory that holds its file. Each case MUST state the request and
  whether that request must reach this skill or must not, and MAY state the
  language it is written in. A set that is present and cannot be read MUST be an
  error and MUST NOT be treated as an empty set.
- **OSKA-SKL-26** — A conformant implementation MUST run every evaluation set in
  the catalog whenever the catalog changes, and a failing case MUST fail that
  run. A case that must reach its own skill passes only when that skill is the
  top of the ranking and the ranking is decisive enough for the runtime to
  choose it; a case that must not reach it passes only when that skill is not
  the top of the ranking.
- **OSKA-SKL-27** — An evaluation MUST rank with the ranking the runtime uses
  and MUST NOT reimplement scoring. Proposing cases for a skill MUST draw them
  from the declared triggers of the skills nearest it and MUST NOT write
  anything into the catalog.

## Rationale

The obvious alternative is to put procedures in code. A procedure is a
function; a capability is what the function calls. This is how most automation
is written and it has one fatal property for an agent system: the people who
own the procedure cannot read it, and the system cannot reason about it. An
on-call engineer knows when a restart is the wrong move and a rollback is the
right one. That knowledge belongs in the artifact, next to the declarations,
where it can be reviewed by the person who holds it and read by the model that
executes it. A skill file is a runbook that happens to be machine-checkable.

The second alternative is to keep the procedures in prose and let the model
decide which tools to call. That collapses the distinction between description
and permission. In OSKA the body is description and the frontmatter is
permission, and the validator enforces the split: no matter what the steps
paragraph says, a write not listed in `capabilities.writes` cannot run. This is
why `OSKA-SKL-14` exists as a requirement rather than as an implementation
note. It is the property that makes the body safe to write freely.

Releasing the catalog as an artifact (`OSKA-SKL-18` to `OSKA-SKL-24`) is
optional because for one process it buys nothing: a directory it owns is already
a single authority, and reloading it on a fingerprint is the shortest path from
an edit to a served change. It stops being optional the moment a second process
serves the same catalog. Two replicas reading two disks are two catalogs
that happen to agree most of the time, and the moments they disagree are exactly
the moments a change is being rolled out. Naming the catalog by its content
turns "are we serving the same thing?" from an argument about file timestamps
into a comparison of two strings, and turns a rollback into serving an
identifier that is still sitting there.

The identifier is deliberately not a version number a person chooses. A chosen
name can be reused, and a catalog that was served under a name someone later
reused cannot be recovered from the audit trail. A content hash cannot be
reused for different content, which is the property a receipt needs from it.

Deriving the system's self-description from the catalog (`OSKA-SKL-11`) is the
requirement implementers most often want to relax, because a hand-written
greeting reads better. Relaxing it reintroduces exactly the failure the
standard exists to remove: the system's claim about itself and its actual
capability become two artifacts that must be kept in sync by hand, and they
will not be. Derived phrasing is a small, permanent cost; capability drift is
an unbounded one.

## In the reference implementation

- `src/oska/catalog/` — `loader.py`, `registry.py`, `validator.py`,
  `resolver.py`.
- `src/oska/engine/factory.py` — the assembly, which raises `CatalogError`
  rather than serve a catalog carrying an error.
- `src/oska/contracts/skill.py` — the frontmatter, sections and skill models.
- `schemas/skill.schema.json`, `schemas/policy.schema.json` — the normative
  machine-readable contracts.
- `src/oska/planner/planner.py` — `validate_plan`, which takes the inputs
  already collected from the person and reports every field a procedure
  reserves to them that the model filled in anyway.
- `src/oska/planner/prompts.py` — the planning prompt, which lists the
  reserved fields and states the rule before the check has to.
- `examples/ops/skills/` — the reference domain: four entity skills, four
  integration skills, six procedures and one composite.
