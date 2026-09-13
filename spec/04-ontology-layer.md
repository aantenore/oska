# 04 Ontology layer (L2)

## Purpose

L2 declares what exists. It is the layer that makes "the search service" a
thing with a name, an identifier, an environment and a status, rather than a
noun phrase in a prompt. Everything above it refers to records by entity type
and identifier, and every operation in L1 is bound to one of these types.

The layer also carries the shared vocabulary: the words the person, the
catalogue and the model all use for the same thing. A system whose ontology
calls a record an "incident" and whose replies call it a "ticket" will drift,
because the model will be asked to translate between two vocabularies nobody
declared.

## Modules

### Entity Type

Responsibility: declare a record class — its fields, which field identifies an
instance, which field carries its state, which states are allowed, and which
other entity types it relates to.

Interface: a lookup by entity identifier returning the declaration. An entity
declares no operations and no triggers: nothing routes to an entity, it is
referenced by the procedures that touch it.

```text
EntityType:
  id: string                 # "entity.<name>"
  fields: [EntityField]      # exactly one carries identifier: true
  state_field: string | null
  states: [string]           # allowed values of state_field, may be empty
  relations: [Relation]

EntityField:
  name: string
  type: string
  identifier: boolean
  description: string
```

The identifier is not a separate field of the declaration: it is the one entry
of `fields` whose `identifier` flag is set. `state_field` MUST name a declared
field, and a declared `state_field` MUST carry a non-empty `states` list.

### Relations

Responsibility: declare typed edges between entity types, with cardinality, so
the graph can answer questions no single entity can.

```text
Relation:
  name: string               # "runs_on", "opened_for"
  target: string             # entity id
  cardinality: "one" | "many"
  description: string
```

A relation has no `source` field: the source is the entity type that declares
it. Relations are declared in one direction; the inverse, when it is wanted, is
declared on the other entity.

### Operation Binding

Responsibility: record which operations act on which entity type, and which
state a write leaves that entity in, derived from the integration declarations
in L1 and exposed as a queryable relation rather than as a field to be read one
integration at a time.

```text
OperationSpec:                 # the part of an L1 declaration L2 reads
  name: string
  write: boolean
  entity: string | null        # entity id this operation acts on
  transitions: {field: value}  # state the entity is left in by this operation

binding(entity_id) -> [{integration_id, operation, write, transitions}]
```

An operation's `entity` MUST be one of the entity types the declaring
integration lists. A `transitions` entry MUST be keyed by the state field of
that entity type and MUST name one of its declared states.

### Ontology Graph

Responsibility: answer structural questions from the declarations alone, with
no model involved.

The graph is built from the published skills of one state of the catalogue and
carries that state's fingerprint. It is derived, not authored: every node and
every edge stands for something a skill declares, an unpublished skill
contributes nothing, and a reference to something the catalogue does not hold is
dropped rather than invented.

```text
Node:
  id: string
  kind: entity | integration | operation | procedure | composite
  label: string
  attributes: object          # what the declaration carries, by kind

Edge:
  kind: relates_to | acts_on | declares | uses | touches | contains
  source: string
  target: string
  attributes: object

OntologyGraph:
  from_catalogue(catalogue) -> OntologyGraph
  node(node_id) -> Node | null
  nodes_of(kind) -> [Node]
  edges_from(node_id, kind?) -> [Edge]
  edges_to(node_id, kind?) -> [Edge]
  fingerprint: string
```

One edge kind per declared link: `relates_to` for a relation between two entity
types, `declares` from an integration to each operation it exposes, `acts_on`
from an operation to the entity type it is bound to, `uses` from a procedure to
an integration, `touches` from a procedure to an entity type, and `contains`
from a composite to each member procedure, in declared order.

The queries read the graph and reach back to nothing:

```text
operations_on(graph, entity_id, writes_only?) -> [Node]   # operation nodes
writers_of(graph, entity_id) -> [Node]                    # integration nodes
procedures_for(graph, entity_id, writes_only?) -> [Node]  # procedures, not composites
related(graph, entity_id) -> [(relation, target, cardinality)]
path(graph, from_entity, to_entity) -> [entity_id] | null # shortest relation chain
vocabulary(graph) -> {entity_id: {identifier, state_field, states, fields, description}}
```

These queries serve four consumers: the validator (does everything referenced
exist?), the capability resolver (what can be done with what was just read?),
the operator asking what the system can reach, and the export below.

### Ontology Cache

Responsibility: hold one graph and rebuild it when the catalogue changes.

```text
OntologyCache:
  graph() -> OntologyGraph     # rebuilt when the catalogue fingerprint moves
```

Building the graph is a read over declarations already in memory, so a cache
keyed on the catalogue fingerprint is enough: a reload of the catalogue
invalidates it, and nothing else does.

### Export

Responsibility: render the graph for something other than a query.

```text
to_json(graph) -> object
from_json(object) -> OntologyGraph     # round-trips to_json
to_mermaid(graph, include_operations?) -> string
```

The JSON rendering is the graph itself as plain data, so a reader outside the
runtime sees exactly the nodes and edges the queries see. The diagram rendering
groups nodes into one subgraph per kind and labels each edge with its kind, or
with the relation name and cardinality where it has one.

### Vocabulary

Responsibility: hold the preferred term for each entity type and state, and the
synonyms that route to it, so the prompt text, the reply text and the catalogue
agree.

In the reference implementation the preferred terms are the declared names
themselves: `vocabulary(graph)` returns, per entity type, the identifier field,
the state field, the declared states, the field names and the declared
description. There is one name per entity type and one per state because the
declaration holds one; any other wording is a synonym carried by a trigger, not
a second name.

### Validation codes

The cross-reference checks of L3 report ontology failures under their own
codes, so a catalogue that does not describe a coherent world fails to load
with a reason rather than with a schema error.

| Code | Raised when |
| --- | --- |
| `ontology_unknown_state_field` | `state_field` does not name a declared field |
| `ontology_missing_states` | a state field declares no states, or states are declared with no state field |
| `ontology_unknown_relation_target` | a relation targets something that is not an entity type in the catalogue |
| `ontology_unpublished_relation_target` | a relation targets an entity type that is not published |
| `ontology_unbound_operation` | an operation of a published integration declares no entity |
| `ontology_unlisted_operation_entity` | an operation is bound to an entity the declaring integration does not list |
| `ontology_transition_without_entity` | an operation declares transitions without an entity in the catalogue |
| `ontology_unknown_transition_field` | a transition is not keyed by the state field of its entity |
| `ontology_unknown_transition_state` | a transition names a value that is not a declared state |

## Invariants

- Every entity referenced anywhere in the catalogue exists and is published.
- Every relation targets an entity that exists and is published.
- An operation's entity binding names an entity the integration itself
  declares, and a declared transition names the state field and one of the
  declared states of that entity.
- Exactly one field of an entity is the identifier.
- When an entity declares a state field, the declared states are the closed set
  of values that field may hold; a read-back that returns a state outside the
  set is a verification failure, not a new state.
- The graph holds nothing that no skill declares: it is built from published
  declarations, it carries the identity of the catalogue state it was built
  from, and a reference the catalogue does not hold is dropped, never invented.
- The ontology is data, not code. Adding an entity is adding a declaration.
- The ontology declares no behaviour. It says what exists and how things
  relate; what can be done lives in L1 and L3.

## Requirements

- **OSKA-ONT-1** — Every entity type MUST declare its fields and MUST designate
  exactly one field as its identifier.
- **OSKA-ONT-2** — An entity type that has a lifecycle MUST declare its state
  field and the closed set of states that field may hold.
- **OSKA-ONT-3** — Every entity identifier referenced by a skill, an operation
  or a relation MUST resolve to a published entity type; an unresolved
  reference MUST fail validation.
- **OSKA-ONT-4** — Every relation MUST declare source, target and cardinality,
  and both endpoints MUST be published entity types.
- **OSKA-ONT-5** — Every declared operation MUST be bound to exactly one entity
  type, and that entity type MUST be one the declaring integration lists.
- **OSKA-ONT-6** — The ontology graph MUST answer `operations_on`, `writers_of`
  and `procedures_for` from declarations alone, without consulting a model
  provider.
- **OSKA-ONT-7** — An entity type MUST NOT declare operations or triggers.
- **OSKA-ONT-8** — The vocabulary MUST provide exactly one preferred term per
  entity type and per declared state; any additional wording is a synonym, not
  a second name.
- **OSKA-ONT-9** — Ontology declarations MUST be versioned and reloadable
  without restarting the runtime.
- **OSKA-ONT-10** — A change that removes an entity type or a state MUST fail
  validation while any published skill or operation still references it.
- **OSKA-ONT-11** — An operation that declares a state transition MUST key it by
  the state field of the entity type it is bound to and MUST name one of that
  type's declared states; anything else MUST fail validation.
- **OSKA-ONT-12** — The ontology graph MUST be derived from published
  declarations only, MUST carry the identity of the catalogue state it was
  built from, and MUST be rebuilt when that identity changes.
- **OSKA-ONT-13** — The ontology MUST be exportable as data that reads back as
  the same graph, so a reader outside the runtime sees the nodes and edges the
  queries answer from.

## Rationale

The obvious alternative is to have no ontology at all: let the schemas of the
operations be the only description of the world, and let the model infer the
rest. This works until two operations disagree. One integration calls the
identifier `service`, another calls it `service_name`, a third returns an
object with a nested `name`. Nothing in the system knows they are the same
thing, so the model is asked to know it, and it will be right most of the time.
"Most of the time" is not an acceptable property for the argument of a write.

Declaring entities separately gives every layer a join key. The validator can
check that an operation's payload field is the identifier of its entity. The
resolver can answer "what can I do with the record I just read" by asking the
graph, not by pattern-matching a schema. The policy engine can write a
condition over `service.environment` because there is a declared field with
that name, on a declared type, populated by a read the plan already performed.
Without the ontology each of those becomes a string convention, and string
conventions decay silently.

The second alternative is a heavier one: a full knowledge graph with
inference, subsumption and reasoning. OSKA deliberately stops short. The graph
here answers four structural queries and holds a vocabulary. It infers nothing.
The reason is the same reason the plan is validated rather than trusted:
anything the system derives by inference is something a human did not declare,
and the governance layer would then be enforcing a rule nobody wrote. Entities,
relations, bindings and words are enough to make every other layer precise, and
they are small enough that a reviewer can read them all.

## In the reference implementation

- `src/opsdeck/ontology/graph.py` — `Node`, `Edge`, `OntologyGraph` and
  `OntologyCache`, built over the published catalogue and keyed on its
  fingerprint.
- `src/opsdeck/ontology/queries.py` — `operations_on`, `writers_of`,
  `procedures_for`, `related`, `path` and `vocabulary`.
- `src/opsdeck/ontology/export.py` — `to_json`, `from_json` and `to_mermaid`.
- `src/opsdeck/contracts/skill.py` — `EntityField`, `Relation`, the entity
  frontmatter fields `state_field` and `states`, and the `entity` and
  `transitions` fields of `OperationSpec`.
- `examples/ops/skills/entity/` — the reference domain's entity declarations.
- `src/opsdeck/catalog/validator.py` — the cross-reference checks that enforce
  `OSKA-ONT-3`, `OSKA-ONT-5` and `OSKA-ONT-11`, reported under the
  `ontology_*` codes listed above.
- `src/opsdeck/governance/verification.py` — where the closed set of states is
  read at run time and not only at validation. The verifier resolves the entity
  the executed write is bound to, turns a declared transition into the expected
  value for the state field when the `expect` block does not already name that
  field, and fails the read-back with `state_outside_declared_set` when the
  value it returns is not one the entity declares.
- `src/opsdeck/cli/main.py` — `opsdeck ontology`, which prints the graph as a
  summary, as JSON or as a diagram.
