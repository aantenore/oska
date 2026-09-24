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
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from jsonschema import Draft202012Validator
from pydantic import ValidationError

from oska.catalog.registry import SkillRegistry
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
    },
}

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
        registry: SkillRegistry,
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
    ) -> Plan:
        """Ask for the operations of one procedure, and repair once if they do not hold up."""
        system = prompts.planner_system(
            hydrated,
            self._resolver.declared_scope(),
            ontology=self._vocabulary(),
        )
        messages = list(conversation)
        if collected_inputs:
            known = json.dumps(collected_inputs, sort_keys=True, ensure_ascii=False)
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
        registry: SkillRegistry | None = None,
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
    ) -> str:
        """Phrase the reply from the outcomes of the turn and nothing else.

        The labels of `actions` are overwritten in place with whatever comes
        back for them, so the wording a turn offers is in the language the turn
        answered in without a second call being made to get it there.
        """
        brief: dict = {
            "reply_intent": reply_intent,
            "outcomes": [outcome.model_dump(mode="json") for outcome in outcomes],
            "actions": [{"key": action.key, "label": action.label} for action in actions],
        }
        if preferences:
            brief["presentation_preferences"] = list(preferences)
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
        answer = self._provider.complete_json(system, messages, PLAN_SCHEMA)
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
    return issues


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


def _description(registry: SkillRegistry, skill_id: str) -> str:
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
