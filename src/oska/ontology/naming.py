"""How an entity is named and shown, and how a procedure binds to what was shown.

An entity may say how one of its records is called (`noun`), how several are
called (`plural`), which of its fields tell one shown record from another
(`display_fields`) and which of its fields a comparative may refine on
(`comparable_fields`). A procedure may say what it acts on (`subject`) and what
a presentation surface shows at each moment of a turn (`presentation`). This
module reads those declarations and nothing else: it is shared by the catalog
checks, which refuse a declaration that names nothing, and by the runtime, which
answers the declaration from evidence.

A presentation binding is a string. One that does not start with `$` is copied
as written. One that does names evidence, in exactly one of four forms:

- `$items.<field>`: the list of that field over the records a read returned.
- `$item.<field>`: that field of the one record a read returned.
- `$selection`: the identifier of the record the turn resolved from the focus.
- `$receipt.<field>`: a field of a verified receipt, or of the payload it
  verified.

What a person typed is not among them. A value somebody gave is evidence only
once a write carried it and its read-back confirmed it, and then it is reached
as `$receipt.<field>`; `$inputs.<field>` is refused with a code that says so.

A field whose declared type is `list` (or `array`) holds several values. It is
matched by membership: a wanted value matches when it equals one of them, never
when it is a piece of one.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from oska.contracts.skill import OperationSpec, Skill, SkillFrontmatter, SkillType

__all__ = [
    "EVIDENCE_KINDS",
    "MULTI_VALUED_TYPES",
    "RECEIPT_FIELDS",
    "UNVERIFIED_KINDS",
    "WARNING_CODES",
    "Binding",
    "binding_issues",
    "entity_issues",
    "identifier_names",
    "is_multi_valued",
    "parse_binding",
    "plural_of",
    "noun_of",
    "records_in",
    "short_name",
    "subject_field",
    "subject_issues",
]

MULTI_VALUED_TYPES = frozenset({"list", "array"})
# A binding that can never be answered, though it names nothing wrong: the
# directive it belongs to is dropped at run time, and the catalog still loads.
WARNING_CODES = frozenset({"presentation_receipt_without_write"})
EVIDENCE_KINDS = ("items", "item", "selection", "receipt")
# Once part of the grammar, and refused by name: what a person typed is not
# evidence until a verified receipt carries it.
UNVERIFIED_KINDS = frozenset({"inputs"})
RECEIPT_FIELDS = ("receipt_id", "operation", "integration_id", "verification", "status")
_NEEDS_NO_FIELD = frozenset({"selection"})
_RECORD_KINDS = frozenset({"items", "item"})


@dataclass(frozen=True)
class Binding:
    """One `$kind.field` reference to evidence."""

    kind: str
    field: str = ""


def parse_binding(value: str) -> Binding | None:
    """The evidence a binding names, or None for a value copied as written."""
    if not isinstance(value, str) or not value.startswith("$"):
        return None
    kind, _, field = value[1:].partition(".")
    return Binding(kind=kind, field=field)


def short_name(entity_id: str) -> str:
    """The key a read returns an entity under: `entity.alert` is `alert`."""
    return entity_id.split(".", 1)[-1]


def noun_of(entity: SkillFrontmatter) -> str:
    """How one record of the entity is named."""
    return entity.noun or short_name(entity.id)


def plural_of(entity: SkillFrontmatter) -> str:
    """How several records of the entity are named."""
    return entity.plural or f"{noun_of(entity)}s"


def identifier_names(entity: SkillFrontmatter) -> list[str]:
    """The fields the entity declares as identifiers."""
    return [field.name for field in entity.fields if field.identifier]


def is_multi_valued(entity: SkillFrontmatter, name: str) -> bool:
    """Whether a declared field holds several values."""
    field = entity.field(name)
    return field is not None and field.type in MULTI_VALUED_TYPES


def records_in(entity_id: str, data: dict) -> list[dict] | None:
    """The records of one entity a result holds, or None when it holds none.

    One record comes back under the entity's short name, several under that
    name with an `s`; that is the convention every read of the runtime keeps.
    """
    short = short_name(entity_id)
    single = data.get(short)
    if isinstance(single, dict):
        return [single]
    listed = data.get(f"{short}s")
    if isinstance(listed, list):
        records = [item for item in listed if isinstance(item, dict)]
        return records if records else None
    return None


def subject_field(entity: Skill, operations: Sequence[OperationSpec]) -> tuple[str, str] | None:
    """The payload field through which these operations name one record of the entity.

    Returned with the identifier it carries. Three declarations answer it, in
    this order: an input named like an identifier of the entity; a read-back
    that compares `<entity>.<identifier>` (or `<entity>s.<n>.<identifier>`)
    with `$payload.<field>`; an input named like the entity itself, carrying
    its first identifier.
    """
    names = identifier_names(entity.frontmatter)
    if not names:
        return None
    short = short_name(entity.id)
    accepted = {
        name for spec in operations for name in _properties(spec.input_schema)
    }
    for name in names:
        if name in accepted:
            return name, name
    for spec in operations:
        if spec.verify is None:
            continue
        for path, source in spec.verify.expect.items():
            parts = path.split(".")
            head, tail = parts[0], parts[-1]
            if head in (short, f"{short}s") and tail in names and isinstance(source, str):
                field = source.removeprefix("$payload.")
                if field != source and field in accepted:
                    return field, tail
    return (short, names[0]) if short in accepted else None


def entity_issues(entity: Skill) -> list[tuple[str, str]]:
    """Every shown or comparable field is a field the entity declares."""
    frontmatter = entity.frontmatter
    issues: list[tuple[str, str]] = []
    for code, listed in (
        ("presentation_unknown_display_field", frontmatter.display_fields),
        ("presentation_unknown_comparable_field", frontmatter.comparable_fields),
    ):
        issues.extend(
            (code, f"{name} is not a declared field of {entity.id}")
            for name in listed
            if frontmatter.field(name) is None
        )
    return issues


def subject_issues(
    procedure: Skill, by_id: dict[str, Skill], operations: Sequence[OperationSpec]
) -> list[tuple[str, str]]:
    """A subject names an entity of the catalog that the procedure can name a record of."""
    subject = procedure.frontmatter.subject
    if subject is None:
        return []
    entity = by_id.get(subject.entity)
    if entity is None or entity.type is not SkillType.entity:
        return [("subject_unknown_entity", f"{subject.entity} is not an entity in the catalog")]
    issues: list[tuple[str, str]] = []
    if procedure.type is SkillType.runbook and subject.entity not in procedure.frontmatter.entities:
        issues.append(
            ("subject_unlisted_entity", f"{subject.entity} is not one of the entities listed")
        )
    if subject.joins == "focus" and subject_field(entity, operations) is None:
        issues.append(
            (
                "subject_unjoinable",
                f"no operation of this procedure accepts a field naming a {short_name(entity.id)}",
            )
        )
    return issues


def binding_issues(
    procedure: Skill,
    by_id: dict[str, Skill],
    operations: Sequence[OperationSpec],
    writes: Sequence[OperationSpec],
) -> list[tuple[str, str]]:
    """Every presentation binding names evidence the runtime can hold for this procedure."""
    frontmatter = procedure.frontmatter
    if not frontmatter.presentation:
        return []
    record_fields = _record_fields(procedure, by_id, operations)
    written = {name for spec in writes for name in _properties(spec.input_schema)}
    issues: list[tuple[str, str]] = []
    for moment, declared in frontmatter.presentation.items():
        for key, raw in declared.bind.items():
            binding = parse_binding(raw)
            if binding is None:
                continue
            where = f"{moment}.{key}: {raw}"
            issue = _binding_issue(binding, record_fields, written)
            if issue is not None:
                issues.append((issue[0], f"{where} {issue[1]}"))
    return issues


def _binding_issue(
    binding: Binding,
    record_fields: set[str],
    written: set[str],
) -> tuple[str, str] | None:
    if binding.kind in UNVERIFIED_KINDS:
        return (
            "presentation_unverified_input",
            "echoes what a person typed, which is not evidence; bind the value a verified "
            "write carried as $receipt.<field>",
        )
    if binding.kind not in EVIDENCE_KINDS:
        return ("presentation_unknown_evidence", f"names no known evidence {EVIDENCE_KINDS}")
    if binding.kind in _NEEDS_NO_FIELD:
        if binding.field:
            return ("presentation_malformed_binding", "takes no field")
        return None
    if not binding.field:
        return ("presentation_malformed_binding", "names no field")
    if binding.kind in _RECORD_KINDS and binding.field not in record_fields:
        return (
            "presentation_unknown_field",
            "is not a field of the subject's entity, or of an entity this procedure reads",
        )
    if binding.kind == "receipt":
        if not written:
            return ("presentation_receipt_without_write", "but this procedure writes nothing")
        if binding.field not in written and binding.field not in RECEIPT_FIELDS:
            return (
                "presentation_unknown_receipt_field",
                "is neither a receipt field nor a field of a payload this procedure writes",
            )
    return None


def _record_fields(
    procedure: Skill, by_id: dict[str, Skill], operations: Sequence[OperationSpec]
) -> set[str]:
    """The fields a `$item` or `$items` binding may name for this procedure.

    A procedure that names its subject is shown records of that entity alone;
    one that does not is shown records of whatever its reads return.
    """
    subject = procedure.frontmatter.subject
    reads = set(procedure.frontmatter.capabilities.reads)
    entity_ids = (
        {subject.entity}
        if subject is not None
        else {spec.entity for spec in operations if spec.name in reads and spec.entity}
    )
    names: set[str] = set()
    for entity_id in entity_ids:
        entity = by_id.get(entity_id)
        if entity is not None and entity.type is SkillType.entity:
            names |= {field.name for field in entity.frontmatter.fields}
    return names


def _properties(schema: dict) -> set[str]:
    properties = schema.get("properties") if isinstance(schema, dict) else None
    return set(properties) if isinstance(properties, dict) else set()
