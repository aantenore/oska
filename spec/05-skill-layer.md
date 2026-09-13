# 05 Skill layer (L3)

## Purpose

L3 holds what the system knows how to do, as files. A skill is a versioned
declarative artefact: typed frontmatter that machines read, and a body written
for the person who owns the procedure. The runtime is generic; the catalogue
makes it specific. Adding a capability is adding a file and publishing it.

This layer also computes what the system says about itself. The greeting, the
refusal, the list of next actions after a read: all derived from the published
catalogue, never written by hand.

## Modules

### Skill artefact

Responsibility: carry one declaration. Four types, one artefact format.

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
  policy: string | null         # path to the policy document

SkillBody:
  purpose, preconditions, steps, verification, rollback, extra
```

The body is prose for a human. The frontmatter is the contract. Nothing in the
body grants a capability.

### Catalogue

Responsibility: load every skill from one or more roots, keep them addressable
by identifier, and reload when the files change.

```text
Catalogue:
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

### Validator

Responsibility: decide whether a catalogue is coherent, and report every
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

Responsibility: turn the published catalogue into the words the system uses
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
the catalogue.

## Invariants

- A procedure with a non-empty `capabilities.writes` declares a policy.
- A composite declares no policy; each member runs under its own.
- A composite's capabilities equal the union of its members' capabilities.
- Triggers exist only on procedures and composites.
- The highest published version wins; a draft never routes.
- The catalogue reloads on a change of file fingerprint without a restart.
- The resolver names only published skills. A retired procedure disappears from
  what the system offers in the same reload.
- A skill body cannot widen a capability. Only frontmatter grants anything.

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
- **OSKA-SKL-6** — The catalogue MUST resolve an identifier to the highest
  published version, and MUST NOT return draft or retired versions from
  `published`.
- **OSKA-SKL-7** — The catalogue MUST detect a change to its roots by
  fingerprint and reload without a restart.
- **OSKA-SKL-8** — The validator MUST report every issue it finds, not only the
  first, and each issue MUST identify the skill and the offending path.
- **OSKA-SKL-9** — A catalogue with any validation issue MUST NOT be served to
  the cognition layer.
- **OSKA-SKL-10** — Hydration MUST return the transitive closure of a procedure
  and MUST NOT include skills outside it.
- **OSKA-SKL-11** — Every sentence the system states about its own capability
  MUST be derived from the published catalogue; no capability copy may be
  hard-coded in code, prompt or template.
- **OSKA-SKL-12** — `actions_for_entity` MUST return published procedures that
  hold at least one write on that entity, and MUST exclude read-only procedures
  and composites.
- **OSKA-SKL-13** — A skill file that cannot be parsed MUST be reported as a
  validation issue and MUST NOT be silently skipped.
- **OSKA-SKL-14** — The skill body MUST NOT be able to grant, widen or
  otherwise affect any capability; only frontmatter is binding.

## Rationale

The obvious alternative is to put procedures in code. A procedure is a
function; a capability is what the function calls. This is how most automation
is written and it has one fatal property for an agent system: the people who
own the procedure cannot read it, and the system cannot reason about it. An
on-call engineer knows when a restart is the wrong move and a rollback is the
right one. That knowledge belongs in the artefact, next to the declarations,
where it can be reviewed by the person who holds it and read by the model that
executes it. A skill file is a runbook that happens to be machine-checkable.

The second alternative is to keep the procedures in prose and let the model
decide which tools to call. That collapses the distinction between description
and permission. In OSKA the body is description and the frontmatter is
permission, and the validator enforces the split: no matter what the steps
paragraph says, a write not listed in `capabilities.writes` cannot run. This is
why `OSKA-SKL-14` exists as a requirement rather than as an implementation
note. It is the property that makes the body safe to write freely.

Deriving the system's self-description from the catalogue (`OSKA-SKL-11`) is the
requirement implementers most often want to relax, because a hand-written
greeting reads better. Relaxing it reintroduces exactly the failure the
standard exists to remove: the system's claim about itself and its actual
capability become two artefacts that must be kept in sync by hand, and they
will not be. Derived phrasing is a small, permanent cost; capability drift is
an unbounded one.

## In the reference implementation

- `src/opsdeck/catalog/` — `loader.py`, `registry.py`, `validator.py`,
  `resolver.py`.
- `src/opsdeck/engine/factory.py` — the assembly, which raises `CatalogError`
  rather than serve a catalogue carrying an error.
- `src/opsdeck/contracts/skill.py` — the frontmatter, sections and skill models.
- `schemas/skill.schema.json`, `schemas/policy.schema.json` — the normative
  machine-readable contracts.
- `examples/ops/skills/` — the reference domain: four entity skills, four
  integration skills, six procedures and one composite.
