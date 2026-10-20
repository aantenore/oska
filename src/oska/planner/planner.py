"""Plan construction, plan validation, tie breaking and reply phrasing.

The call that writes a reply carries the actions the turn leaves on offer, each
as a key and the wording its author declared, and may hand every label back in
the language it is answering in. It is the same call either way: a turn that
offers something costs no more model calls than a turn that offers nothing, and
the prompts routing is built from stay in the language the catalog declares,
because routing is asked in every language a person writes in.

Presentation preferences travel the same road and only that road. They reach
the reply writer as facts about how an answer should read. They are not in the
planning prompt, they are not in a payload, and nothing here puts them there.

Two more kinds of fact reach the model, each from the runtime and never from a
model. The refinements the records on screen allow reach the planner next to
the inputs already collected, so a comparative picks among what the shown
records actually differ on. The scope the catalog declares reaches the reply
writer when a request is outside it, so the answer that says so is written in
the language of the conversation by the same one call that writes any reply.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Sequence

from jsonschema import Draft202012Validator
from pydantic import ValidationError

from oska.catalog.registry import Catalog
from oska.catalog.resolver import CapabilityResolver, SuggestedAction, relabel
from oska.contracts.common import Hydrated, Message, Outcome, RankedSkill
from oska.contracts.plan import Plan
from oska.contracts.skill import OperationSpec, Skill
from oska.ontology.graph import OntologyCache
from oska.ontology.queries import related, vocabulary
from oska.planner import prompts
from oska.planner.provider import ModelProvider, ModelProviderError

PLAN_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["runbook_id", "operations", "missing", "reply_intent"],
    "properties": {
        "runbook_id": {"type": "string"},
        "operations": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["integration_id", "operation", "payload", "write", "purpose"],
                "properties": {
                    "integration_id": {"type": "string"},
                    "operation": {"type": "string"},
                    "payload": {"type": "object"},
                    "write": {"type": "boolean"},
                    "purpose": {"type": "string"},
                },
            },
        },
        "missing": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["field", "question"],
                "properties": {
                    "field": {"type": "string"},
                    "question": {"type": "string"},
                },
            },
        },
        "reply_intent": {"type": "string"},
        "language": {
            "type": "string",
            "description": "ISO 639-1 code of the language the person wrote in",
        },
    },
}

def plan_schema(hydrated: Hydrated) -> dict:
    """The plan schema narrowed to what this procedure declares.

    The general schema accepts any string where a procedure, an integration, an
    operation or a missing input is named. Handed to a provider that constrains
    its output to the schema, the narrowed copy leaves the model no way to name
    something the catalog does not declare: the procedure is fixed, and each
    step and each question can only name what the closure holds. The plan is
    still validated afterwards; this makes a valid plan the only one a model
    can write, whatever its size.
    """
    schema = copy.deepcopy(PLAN_SCHEMA)
    properties = schema["properties"]
    properties["runbook_id"] = {"type": "string", "enum": [hydrated.runbook.id]}
    integrations = [skill.id for skill in hydrated.integrations]
    operations = sorted(
        {spec.name for skill in hydrated.integrations for spec in skill.frontmatter.operations}
    )
    step = properties["operations"]["items"]["properties"]
    if integrations:
        step["integration_id"] = {"type": "string", "enum": integrations}
    if operations:
        step["operation"] = {"type": "string", "enum": operations}
    fields = _payload_fields(hydrated)
    if fields:
        # An object with no properties can only be written empty by a provider
        # that holds the output to the schema, and a model that cannot put a value
        # in the payload asks for it instead. Every field a declared operation
        # accepts is offered here; each operation's own schema is checked after.
        step["payload"] = {"type": "object", "additionalProperties": False, "properties": fields}
    accepted = set(human_inputs(hydrated))
    for skill in hydrated.integrations:
        for spec in skill.frontmatter.operations:
            accepted.update((spec.input_schema or {}).get("properties", {}))
    if accepted:
        properties["missing"]["items"]["properties"]["field"] = {
            "type": "string",
            "enum": sorted(accepted),
        }
    return schema


_PORTABLE_KEYWORDS = ("type", "description", "enum", "items", "format")


def _payload_fields(hydrated: Hydrated) -> dict:
    """Every input field the declared operations accept, in a form providers can hold to.

    Only the keywords structured output is broadly held to are kept. A field two
    operations declare differently keeps only its description, and the
    operation's own schema decides afterwards.
    """
    fields: dict[str, dict] = {}
    for skill in hydrated.integrations:
        for spec in skill.frontmatter.operations:
            for name, declared in (spec.input_schema or {}).get("properties", {}).items():
                portable = _portable(declared)
                if name not in fields:
                    fields[name] = portable
                elif fields[name] != portable:
                    fields[name] = {"description": portable.get("description", name)}
    return fields


def _portable(declared: dict) -> dict:
    kept = {key: value for key, value in declared.items() if key in _PORTABLE_KEYWORDS}
    if isinstance(kept.get("items"), dict):
        kept["items"] = _portable(kept["items"])
    return kept


PICK_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["skill_id"],
    "properties": {"skill_id": {"type": "string"}},
}

REPLY_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["reply"],
    "properties": {
        "reply": {"type": "string"},
        "action_labels": {
            "type": "object",
            "description": (
                "The wording of each offered action, by its key, in the language of the"
                " reply. Translate the label and change nothing about what it offers."
                " Leave out any action you cannot phrase, and add no key that was not"
                " offered."
            ),
            "additionalProperties": {"type": "string"},
        },
    },
}


_UNCOLLECTED = object()


class InvalidPlanError(RuntimeError):
    """Raised when a plan still breaks its declarations after one repair."""

    def __init__(self, issues: Sequence[str]) -> None:
        self.issues = list(issues)
        super().__init__("; ".join(self.issues) or "the plan is invalid")


class Planner:
    """The model-backed part of a turn: plan, tie break, reply."""

    def __init__(
        self,
        provider: ModelProvider,
        registry: Catalog,
        resolver: CapabilityResolver,
    ) -> None:
        self._provider = provider
        self._registry = registry
        self._resolver = resolver
        self._ontology = OntologyCache(registry)

    @property
    def provider(self) -> ModelProvider:
        """The model backend this planner asks."""
        return self._provider

    def plan(
        self,
        hydrated: Hydrated,
        conversation: list[Message],
        collected_inputs: dict | None = None,
        *,
        refinements: Sequence[dict] | None = None,
        from_focus: dict | None = None,
        guidance: Sequence[str] = (),
    ) -> Plan:
        """Ask for the operations of one procedure, and repair once if they do not hold up.

        `refinements` are what the records on screen allow a comparative to
        refine on. They are handed over as they are and the model may pick
        among them; none is ever made up here. `from_focus` are the collected
        inputs that name the record the conversation is about rather than
        anything the person typed; they are stated apart from the rest, as the
        default a request that names another record does not take.
        """
        system = prompts.planner_system(
            hydrated,
            self._resolver.declared_scope(),
            ontology=self._vocabulary(),
            guidance=guidance,
        )
        messages = list(conversation)
        focused = dict(from_focus or {})
        if focused:
            about = json.dumps(focused, sort_keys=True, ensure_ascii=False)
            messages.append(Message(role="system", content=f"{prompts.FOCUS_HEADING}: {about}"))
        if refinements:
            allowed = json.dumps(list(refinements), sort_keys=True, ensure_ascii=False)
            messages.append(
                Message(role="user", content=f"{prompts.REFINEMENTS_HEADING}: {allowed}")
            )
        told = {key: value for key, value in (collected_inputs or {}).items() if key not in focused}
        if told:
            known = json.dumps(told, sort_keys=True, ensure_ascii=False)
            messages.append(
                Message(role="user", content=f"Inputs already collected: {known}")
            )
        plan, issues = self._ask_for_plan(system, messages, hydrated, collected_inputs)
        if not issues:
            return _require(plan)
        repaired = [*messages, Message(role="user", content=_repair_note(issues))]
        plan, issues = self._ask_for_plan(system, repaired, hydrated, collected_inputs)
        if issues:
            raise InvalidPlanError(issues)
        return _require(plan)

    def _vocabulary(self) -> dict:
        """The declared vocabulary, each entity carrying the relations it declares."""
        graph = self._ontology.graph()
        entries = vocabulary(graph)
        for entity_id, entry in entries.items():
            entry["relations"] = related(graph, entity_id)
        return entries

    def pick(
        self,
        query: str,
        candidates: list[RankedSkill],
        registry: Catalog | None = None,
    ) -> str:
        """Choose one candidate id; an answer outside the list falls back to the first."""
        if not candidates:
            raise ValueError("pick needs at least one candidate")
        source = registry or self._registry
        ids = [candidate.skill_id for candidate in candidates]
        lines = [f"{skill_id}: {_description(source, skill_id)}" for skill_id in ids]
        answer = self._provider.complete_json(
            prompts.pick_system(lines),
            [Message(role="user", content=query)],
            PICK_SCHEMA,
        )
        chosen = answer.get("skill_id")
        if isinstance(chosen, str) and chosen in ids:
            return chosen
        return ids[0]

    def write_reply(
        self,
        outcomes: list[Outcome],
        reply_intent: str,
        actions: Sequence[SuggestedAction],
        language: str,
        conversation: list[Message] | None = None,
        preferences: Sequence[str] = (),
        facts: Sequence[str] = (),
    ) -> str:
        """Phrase the reply from the outcomes of the turn and nothing else.

        The labels of `actions` are overwritten in place with whatever comes
        back for them, so the wording a turn offers is in the language the turn
        answered in without a second call being made to get it there. `facts`
        are statements the runtime derived from the catalog, such as the scope
        it declares, which the reply may state as they are.
        """
        brief: dict = {
            "reply_intent": reply_intent,
            "outcomes": [outcome.model_dump(mode="json") for outcome in outcomes],
            "actions": [{"key": action.key, "label": action.label} for action in actions],
        }
        if preferences:
            brief["presentation_preferences"] = list(preferences)
        if facts:
            brief["facts"] = list(facts)
        messages = [
            *(conversation or []),
            Message(role="user", content=json.dumps(brief, sort_keys=True, ensure_ascii=False)),
        ]
        answer = self._provider.complete_json(
            prompts.reply_system(language), messages, REPLY_SCHEMA
        )
        reply = answer.get("reply")
        if not isinstance(reply, str) or not reply.strip():
            raise ModelProviderError("the model returned no reply text")
        relabel(actions, _returned_labels(answer.get("action_labels")))
        return reply.strip()

    def _ask_for_plan(
        self,
        system: str,
        messages: list[Message],
        hydrated: Hydrated,
        collected_inputs: dict | None = None,
    ) -> tuple[Plan | None, list[str]]:
        answer = self._provider.complete_json(system, messages, plan_schema(hydrated))
        try:
            plan = Plan.model_validate({**answer, "runbook_id": hydrated.runbook.id})
        except ValidationError as exc:
            return None, [f"the answer is not a plan: {_first_error(exc)}"]
        return plan, validate_plan(plan, hydrated, collected_inputs)


def validate_plan(
    plan: Plan,
    hydrated: Hydrated,
    collected_inputs: dict | None = None,
) -> list[str]:
    """Every way this plan disagrees with what the catalog declares.

    `collected_inputs` is what the person has already been asked for and has
    answered. It is the only source a human input may come from: a field the
    procedure reserves to a person and the model filled in anyway is reported
    here, whatever else about the plan holds up.
    """
    issues: list[str] = []
    declared = {skill.id: skill for skill in hydrated.integrations}
    asked = {item.field for item in plan.missing}
    human = human_inputs(hydrated)
    collected = dict(collected_inputs or {})
    for position, operation in enumerate(plan.operations, start=1):
        where = f"operation {position} ({operation.integration_id} {operation.operation})"
        integration = declared.get(operation.integration_id)
        if integration is None:
            issues.append(
                f"{where}: integration {operation.integration_id} is not declared here"
            )
            continue
        spec = integration.frontmatter.operation(operation.operation)
        if spec is None:
            issues.append(
                f"{where}: {integration.id} declares no operation {operation.operation}; "
                f"it declares {_names(integration)}"
            )
            continue
        if operation.write != spec.write:
            issues.append(
                f"{where}: declared as a {'write' if spec.write else 'read'}, "
                f"but the plan marks it write={operation.write}"
            )
        issues.extend(_payload_issues(where, operation.payload, spec, asked))
        issues.extend(_human_input_issues(where, operation.payload, human, collected))
    issues.extend(_missing_issues(plan, declared, human))
    return issues


def _missing_issues(plan: Plan, declared: dict[str, Skill], human: set[str]) -> list[str]:
    """A question is only worth asking about an input some declared operation takes.

    A model left to itself asks for whatever looks unknown to it, including the
    runtime's own identifiers. What a person is asked must be a field one of the
    declared operations accepts, or one the procedure reserves to a person.
    """
    accepted = set(human)
    for integration in declared.values():
        for spec in integration.frontmatter.operations:
            accepted.update((spec.input_schema or {}).get("properties", {}))
    return [
        f"`missing` asks for {item.field!r}, which no declared operation takes; ask only for "
        f"inputs the declared operations accept: {', '.join(sorted(accepted)) or 'none'}"
        for item in plan.missing
        if item.field not in accepted
    ]


def human_inputs(hydrated: Hydrated) -> set[str]:
    """The payload fields the procedures of this goal reserve to a person."""
    fields = set(hydrated.runbook.frontmatter.human_inputs)
    for member in hydrated.runbooks:
        fields.update(member.frontmatter.human_inputs)
    return fields


def _human_input_issues(
    where: str,
    payload: dict,
    human: set[str],
    collected: dict,
) -> list[str]:
    return [
        f"{where}: {field!r} is a human input and this value was not given by the person;"
        " leave it out of the payload and list it in `missing` with the question to ask"
        for field in sorted(human)
        if field in payload and payload[field] != collected.get(field, _UNCOLLECTED)
    ]


def _payload_issues(
    where: str,
    payload: dict,
    spec: OperationSpec,
    asked: set[str],
) -> list[str]:
    schema = spec.input_schema or {}
    if not schema:
        return []
    issues: list[str] = []
    for field in schema.get("required", []):
        if field not in payload and field not in asked:
            issues.append(
                f"{where}: required input {field!r} is not in the payload"
                " and not listed in `missing`"
            )
    for error in Draft202012Validator(schema).iter_errors(payload):
        if error.validator == "required":
            continue
        location = ".".join(str(part) for part in error.absolute_path) or "payload"
        issues.append(f"{where}: {location} does not satisfy the input schema: {error.message}")
    return issues


def _returned_labels(value: object) -> dict[str, str]:
    """The labels an answer returned, keeping only the ones that are text."""
    if not isinstance(value, dict):
        return {}
    return {
        str(key): text
        for key, text in value.items()
        if isinstance(text, str) and text.strip()
    }


def _repair_note(issues: Sequence[str]) -> str:
    listed = "\n".join(f"- {issue}" for issue in issues)
    return (
        "That plan cannot run. Fix these points and answer with the corrected plan:\n"
        f"{listed}"
    )


def _names(integration: Skill) -> str:
    return ", ".join(spec.name for spec in integration.frontmatter.operations) or "nothing"


def _description(registry: Catalog, skill_id: str) -> str:
    skill = registry.get(skill_id)
    return skill.frontmatter.description if skill is not None else skill_id


def _first_error(exc: ValidationError) -> str:
    errors = exc.errors()
    if not errors:
        return str(exc)
    first = errors[0]
    location = ".".join(str(part) for part in first.get("loc", ())) or "root"
    return f"{location}: {first.get('msg', '')}"


def _require(plan: Plan | None) -> Plan:
    if plan is None:
        raise InvalidPlanError(["the answer is not a plan"])
    return plan
