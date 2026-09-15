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

### Entity Lifecycle

Responsibility: declare, as a state machine, the moves an entity type may make
between its states, and the operation that causes each one.

The states of an entity say what its state field may hold. They do not say how
it gets from one to another, which of them means the object is finished, or
which of them it never leaves. A lifecycle says exactly that, and it is part of
the ontology rather than of a procedure because it is a fact about the record,
true whichever procedure is running.

```text
Lifecycle:                     # declared on the entity type
  initial_state: string        # the state an instance starts in
  final_states: [string]       # states the entity never leaves
  state_read: string           # the read the current state is taken from
  lifecycle: [LifecycleMove]

LifecycleMove:
  from: [string]               # states this move is legal from
  to: string                   # state the move lands in
  operation: string            # the write that causes it
  required: boolean            # this move completes the object
  offered: boolean             # the runtime may suggest this move
```

Two flags qualify a move, and they are independent of each other.

`required` marks the move that completes the object: until it is made, the
object is not yet what the person asked for. An access request recorded as
`open` is unfinished work even though nothing is wrong with it, and a runtime
that has just read one says so. At most one move out of any given state may be
required, because "the next step" is singular.

`offered: false` marks a move that is legal and is planned and executed when a
person asks for it, but that the runtime MUST NOT suggest. Legality and advice
are not the same judgement: revoking an access that was granted a moment ago is
a move somebody may have good reason to make, and it is not a move a machine
has any business proposing.

The state a move is judged against is never inferred from the conversation. It
is read back through the operation named by `state_read`, which is a declared
read bound to the same entity type. Between one turn and the next the world
moves: a colleague silences the alert, another rollback lands, a request is
settled by the person who owns it. A state taken from what was said earlier is
a guess, and a write is not a place to guess.

A state is always the state of one object, so a state read carries a second
question beside the first: which object the record that came back is about. One
turn commonly reads several objects of the same type, and a record of the right
type is not therefore a record of the right object. The write names the object —
by the entity's identifier field, or by the field the state read is narrowed by
— and a record answers for it only when every one of those fields the record
carries agrees with the write. A record that disagrees, and a record carrying
nothing to compare, are equally no answer: the declared read runs again for the
object the write names. The failure this forbids is quiet and complete. An
illegal move is judged legal against another object's state, it executes, and
the read-back — which looks at the target's own state — reports it verified.

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
  kind: entity | integration | operation | procedure | composite | state
  label: string
  attributes: object          # what the declaration carries, by kind

Edge:
  kind: relates_to | acts_on | declares | uses | touches | contains | moves_to
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
from a composite to each member procedure, in declared order, and `moves_to`
from one state of an entity type to another. A state is a node of its own,
identified as `<entity id>#<state>`, so that a move is an edge like any other
and a lifecycle is asked of the graph rather than read out of an attribute.
Only the states a lifecycle names become nodes: a value the state field may
hold that no declared move leads through is declared on the entity type and
nothing more.

The queries read the graph and reach back to nothing:

```text
operations_on(graph, entity_id, writes_only?) -> [Node]   # operation nodes
writers_of(graph, entity_id) -> [Node]                    # integration nodes
procedures_for(graph, entity_id, writes_only?) -> [Node]  # procedures, not composites
related(graph, entity_id) -> [(relation, target, cardinality)]
path(graph, from_entity, to_entity) -> [entity_id] | null # shortest relation chain
vocabulary(graph) -> {entity_id: {identifier, state_field, states, fields, description}}
lifecycle_of(graph, entity_id) -> Lifecycle | null         # null when none is declared
legal_moves(graph, entity_id, state) -> [Move]             # offered or not
required_move(graph, entity_id, state) -> Move | null
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
with the relation name and cardinality where it has one. States are left out of
that diagram and rendered underneath it instead: one state diagram per entity
type that declares a lifecycle, naming the operation behind each move and
marking the moves that are required or not offered.

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
| `ontology_lifecycle_without_states` | a lifecycle is declared without a state field or without states |
| `ontology_lifecycle_without_initial_state` | a lifecycle is declared without the state an instance starts in |
| `ontology_unknown_lifecycle_state` | the initial state, a final state or an endpoint of a move is not a declared state |
| `ontology_unknown_lifecycle_operation` | a move names an operation that is not a write bound to this entity type |
| `ontology_missing_state_read` | a lifecycle names no operation to read the current state from |
| `ontology_unknown_state_read` | `state_read` is not a declared read bound to this entity type |
| `ontology_move_from_final_state` | a move leaves a state declared final |
| `ontology_unreachable_state` | a move leads to a state nothing can reach from the initial state |
| `ontology_conflicting_required_move` | more than one required move leaves the same state |

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
- A declared lifecycle names only declared states, starts at one of them, and
  moves only through operations that are writes bound to that entity type.
- A final state has no outgoing move, and every state a move leads to can be
  reached from the initial state. A state that only ever appears as a source is
  an entry the system of record supplies, not an unreachable one.
- The current state of an object is read back through `state_read` before a
  move is judged; it is never inferred from the conversation.
- A state is the state of one object. A record already read answers for the
  object a write names only when it is that same object; otherwise the read
  runs again.
- A move that is legal is not therefore suggested: `offered: false` withholds
  it from the runtime's own mouth without withholding it from the person.
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
- **OSKA-ONT-14** — An entity type MAY declare a lifecycle. A declared lifecycle
  MUST declare the state field, the closed set of states, the initial state and
  the legal moves between states, and each move MUST name the operation that
  causes it; anything else MUST fail validation.
- **OSKA-ONT-15** — Every state named by a lifecycle — the initial state, a
  final state, or either endpoint of a move — MUST be one of the entity type's
  declared states.
- **OSKA-ONT-16** — The operation of every lifecycle move MUST be a write
  declared by an integration and bound to that entity type.
- **OSKA-ONT-17** — A declared lifecycle MUST name, in `state_read`, a read
  operation bound to that entity type, and the runtime MUST take the current
  state of an object from that read. The runtime MUST NOT infer the state from
  the conversation, from an earlier turn, or from the response to the write.
- **OSKA-ONT-18** — A state declared final MUST have no outgoing move, and every
  state a move leads to MUST be reachable from the initial state; a destination
  that is not reachable MUST fail validation and MUST be reported.
- **OSKA-ONT-19** — At most one move out of any given state MUST be marked
  `required`.
- **OSKA-ONT-20** — Before executing a write whose operation is named by the
  lifecycle of an entity the operation is bound to, the runtime MUST read the
  object's current state through `state_read` and MUST refuse the write when no
  declared move leads from that state through that operation. A refusal MUST
  record nothing in the write ledger and MUST state the current state and the
  moves that are legal from it. A move out of a final state MUST always be
  refused.
- **OSKA-ONT-21** — When a write was a declared move, verification MUST fail
  unless the state read back is the state the move declares it lands in.
- **OSKA-ONT-22** — The runtime MUST NOT suggest a move that is not legal from
  the state the object was read in, and MUST NOT suggest a move declared
  `offered: false`. A move declared `required` MUST be named as the step the
  object still needs. Neither flag restricts what may be planned and executed
  when a person asks for it.
- **OSKA-ONT-23** — The state a move is judged against MUST be the state of the
  object the write names. A record an earlier read of the turn returned MAY be
  used only when it is a record of that same object: every field of the write's
  payload that names the object — a field the entity declares its identifier,
  and the field the `state_read` was narrowed by — and that the record carries
  MUST hold the value the payload holds. On any difference, and when the record
  carries no such field at all, the runtime MUST execute `state_read` for the
  object the write names and MUST judge the move against what that read
  reports. Where a read returns a list, the record its state is taken from MUST
  be identified: a list of several records with nothing naming which one MUST
  NOT be read as the state of any of them. A state that could not be read MUST
  NOT be treated as a state every move is legal from; no move is legal from an
  unknown state.

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

The lifecycle raises the same question one level up: why declare the moves at
all, rather than let the policy refuse what should not happen? Because a policy
is written about an operation and a payload, and the thing being refused here
is about the object. "Do not roll back a deployment that has already been rolled
back" is not a rule about rollbacks, it is a fact about deployments, and every
procedure that touches one should be held to it without its author remembering
to write it down. Declaring it once on the entity type also makes it available
to the resolver, which is how the runtime can offer the next step without
anybody writing a sentence about what to offer.

The `required` and `offered` flags exist because two different questions were
being answered by one list. What is legal is a question about the system of
record. What to suggest is a question about judgement, and the two diverge in
both directions: a move can be legal and a bad thing to propose, and a move can
be the only thing left to do and still need a person to ask for it. Keeping
them apart lets an author say "yes, but do not offer it" without inventing a
second catalogue of advice.

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
  `procedures_for`, `related`, `path` and `vocabulary`, and the lifecycle
  queries `lifecycle_of`, `legal_moves` and `required_move`.
- `src/opsdeck/ontology/export.py` — `to_json`, `from_json` and `to_mermaid`.
- `src/opsdeck/contracts/skill.py` — `EntityField`, `Relation`,
  `LifecycleTransition`, the entity frontmatter fields `state_field`, `states`,
  `initial_state`, `final_states`, `lifecycle` and `state_read`, and the
  `entity` and `transitions` fields of `OperationSpec`.
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
- `src/opsdeck/engine/execution.py` — where a write named by a lifecycle is
  judged before it runs: the current state is taken from the reads the plan
  already performed when one of them was about the object the write names, and
  from `state_read` for that object when none was, an illegal move produces an
  `invalid_plan` outcome naming the state and the legal moves, and nothing
  reaches the ledger. `_identity` is where the payload is turned into the
  fields that name the object, and `read_state` in
  `src/opsdeck/governance/verification.py` is where a record is matched against
  them.
- `src/opsdeck/governance/capabilities.py` — `lifecycle_entity`, `move_for` and
  `legal_operations`, which answer which move a write is making.
- `src/opsdeck/catalog/resolver.py` and `src/opsdeck/engine/goals.py` — where
  the suggestions after a read come from the lifecycle: the moves legal from
  the state just read, minus the ones that are not offered, with the required
  one named as the step the object still needs.
- `src/opsdeck/cli/main.py` — `opsdeck ontology`, which prints the graph as a
  summary, as JSON or as a diagram.
