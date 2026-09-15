"""Guardrails: what the catalog declares, what keeps each promise, and where.

The point of the catalog is that the three ways a promise is kept stay apart, so
these tests check all three: a check the chain runs refuses and a turn ends, an
invariant names the module that keeps it where the chain runs nothing, an
instruction reaches the prompt and nothing else, and an identifier no entry
defines is an error rather than a promise nobody keeps.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

from opsdeck.audit.sink import MemorySink
from opsdeck.catalog.registry import SkillRegistry
from opsdeck.catalog.resolver import CapabilityResolver
from opsdeck.catalog.validator import CatalogError, validate, validate_all
from opsdeck.config import ReplyConfig, Settings
from opsdeck.contracts.common import Message, Outcome
from opsdeck.contracts.skill import Skill, SkillType
from opsdeck.engine.factory import build_engine
from opsdeck.engine.reply import ReplyBuilder, guided
from opsdeck.engine.state import SessionState
from opsdeck.governance.guardrails import (
    CHAIN_POINTS,
    GuardrailCatalog,
    GuardrailChain,
    GuardrailError,
    instruction_block,
)
from opsdeck.planner.planner import Planner
from opsdeck.planner.scripted import ScriptedModelProvider

RESTART_REQUEST = "the search service in staging needs a restart, it is wedged again"
CARRIES_A_CREDENTIAL = (
    "restart the search service in staging, the api key is AKIA5T7QZ2WJ4KDLMNOP"
)
CONTAINER_RUNTIME = "integration.container_runtime"

SECRETS = "guardrail.no_secret_material_in_a_turn"
READ_BACK = "guardrail.read_back_before_confirmation"
EXACT_PAYLOAD = "guardrail.exact_payload_approval"
DECLARED_OPERATIONS = "guardrail.declared_operations_only"
IDENTIFIERS = "guardrail.identifiers_are_never_invented"
VERIFIED_ONLY = "guardrail.reply_states_verified_outcomes_only"

# One row per seeded check: the subject it lets through, the subject it refuses,
# and what the turn declares around it.
SEEDED_CHECKS = [
    (SECRETS, "restart the search service in staging", CARRIES_A_CREDENTIAL, {}),
]

# The promises kept at a point the chain does not run, and the module that keeps
# each of them.
SEEDED_INVARIANTS = {
    READ_BACK: ("execution", "opsdeck.governance.verification"),
    EXACT_PAYLOAD: ("execution", "opsdeck.governance.approval"),
    DECLARED_OPERATIONS: ("planning", "opsdeck.planner.planner"),
}


@pytest.fixture
def catalog(repo_root: Path) -> GuardrailCatalog:
    """The catalog this installation ships."""
    return GuardrailCatalog.load(repo_root / "configs" / "guardrails.json")


def _settings(repo_root: Path, state_dir: Path) -> Settings:
    return Settings(
        skills_roots=[repo_root / "examples" / "ops" / "skills"],
        runtime_config=repo_root / "configs" / "runtime.json",
        model_config_path=repo_root / "configs" / "model.json",
        connectors_config=repo_root / "examples" / "ops" / "configs" / "connectors.json",
        schemas_dir=repo_root / "schemas",
        state_dir=state_dir,
        provider="scripted",
    )


def _runtime_config(repo_root: Path, tmp_path: Path, **sections: dict) -> Path:
    """A copy of the runtime configuration with some sections replaced."""
    document = json.loads((repo_root / "configs" / "runtime.json").read_text(encoding="utf-8"))
    document.update(sections)
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _catalog_document(tmp_path: Path, entries: dict, *, beside: Path | None = None) -> Path:
    """A catalog document holding these entries, and the shipped ones when asked.

    A deployment's catalog has to define everything the skills it serves
    declare, so a test that runs a turn over the example catalog starts from
    what that catalog ships and adds to it.
    """
    shipped: dict = {}
    if beside is not None:
        document = json.loads((beside / "configs" / "guardrails.json").read_text(encoding="utf-8"))
        shipped = document["guardrails"]
    path = tmp_path / "guardrails.json"
    path.write_text(json.dumps({"guardrails": {**shipped, **entries}}), encoding="utf-8")
    return path


def _route(kind: str, runbook_id: str | None = None) -> dict:
    return {
        "response": {
            "kind": kind,
            "runbook_id": runbook_id,
            "confidence": 0.9,
            "language": "en",
        }
    }


def _restart_plan() -> dict:
    return {
        "response": {
            "runbook_id": "runbook.service_restart",
            "operations": [
                {
                    "integration_id": CONTAINER_RUNTIME,
                    "operation": "service.status",
                    "payload": {"service": "search"},
                    "write": False,
                    "purpose": "read the current state",
                },
                {
                    "integration_id": CONTAINER_RUNTIME,
                    "operation": "service.restart",
                    "payload": {"service": "search"},
                    "write": True,
                    "purpose": "restart the service",
                },
            ],
            "missing": [],
            "reply_intent": "report the restart",
        }
    }


class _Recorder:
    """A provider that answers from a script and keeps every conversation it saw."""

    name = "recording"

    def __init__(self, inner: ScriptedModelProvider) -> None:
        self._inner = inner
        self.conversations: list[list[Message]] = []

    def complete_json(
        self,
        system: str,
        messages: list[Message],
        schema: dict,
        *,
        temperature: float = 0.0,
    ) -> dict:
        self.conversations.append(list(messages))
        return self._inner.complete_json(system, messages, schema, temperature=temperature)

    def embed(self, texts: list[str]) -> list[list[float]]:
        return self._inner.embed(texts)

    def systems(self) -> list[str]:
        """Every system message of every conversation recorded."""
        return [
            message.content
            for conversation in self.conversations
            for message in conversation
            if message.role == "system"
        ]


def _declare_guardrails(root: Path, kind: str, name: str, ids: list[str]) -> None:
    """Rewrite a skill of a catalog root so that it declares these guardrails."""
    path = root / kind / name / "v1" / "skill.md"
    _, header, body = path.read_text(encoding="utf-8").split("---\n", 2)
    frontmatter = yaml.safe_load(header)
    frontmatter["guardrails"] = ids
    path.write_text(
        "---\n" + yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True) + "---\n" + body,
        encoding="utf-8",
    )


def test_the_catalog_declares_where_each_promise_acts_and_what_keeps_it(
    catalog: GuardrailCatalog,
) -> None:
    """Every entry says its point and its mechanism, and a check names its validator.

    A check is declared only where the chain runs one. Everything else that is
    kept by code says so as an invariant and names the module that keeps it,
    and carries no validator and no parameters, because an entry declaring
    either at a point nothing reads them would read as a control it is not.
    """
    assert set(catalog.ids()) == {
        SECRETS,
        READ_BACK,
        EXACT_PAYLOAD,
        DECLARED_OPERATIONS,
        IDENTIFIERS,
        VERIFIED_ONLY,
    }
    for guardrail in catalog.all():
        enforcement = guardrail.enforcement
        assert enforcement.module, f"{guardrail.id} does not say what keeps it"
        if enforcement.mechanism == "check":
            assert enforcement.validator
            assert enforcement.point in CHAIN_POINTS, guardrail.id
        else:
            assert enforcement.validator is None, guardrail.id
            assert enforcement.parameters == {}, guardrail.id
    assert catalog.require(SECRETS).enforcement.point == "input"
    assert [guardrail.id for guardrail in catalog.all() if guardrail.is_check] == [SECRETS]
    assert {
        guardrail.id: (guardrail.enforcement.point, guardrail.enforcement.module)
        for guardrail in catalog.all()
        if guardrail.is_invariant
    } == SEEDED_INVARIANTS
    assert [
        guardrail.id
        for guardrail in catalog.all()
        if not guardrail.is_check and not guardrail.is_invariant
    ] == [VERIFIED_ONLY, IDENTIFIERS]


def test_a_promise_kept_where_the_chain_runs_nothing_is_declared_an_invariant(
    catalog: GuardrailCatalog,
) -> None:
    """The chain runs checks at two points, and declares nothing to run at the others."""
    assert CHAIN_POINTS == ("input", "output")
    for point in CHAIN_POINTS:
        assert [guardrail.id for guardrail, _ in catalog.checks(point)] == (
            [SECRETS] if point == "input" else []
        )
    for point in ("planning", "execution"):
        assert catalog.checks(point) == []


@pytest.mark.parametrize(
    ("named", "enforcement", "refused_for"),
    [
        (
            "a check the chain runs",
            {
                "point": "input",
                "mechanism": "check",
                "validator": "forbidden_pattern",
                "module": "opsdeck.governance.guardrails",
                "parameters": {"patterns": ["nothing"], "replacement": ""},
            },
            None,
        ),
        (
            "an invariant a module keeps",
            {
                "point": "execution",
                "mechanism": "invariant",
                "module": "opsdeck.governance.ledger",
            },
            None,
        ),
        (
            "an instruction handed to a model",
            {"point": "planning", "mechanism": "instruction", "module": "opsdeck.planner.prompts"},
            None,
        ),
        (
            "an invariant claiming a validator",
            {
                "point": "execution",
                "mechanism": "invariant",
                "validator": "value_in_set",
                "module": "opsdeck.governance.ledger",
            },
            "names no validator",
        ),
        (
            "an invariant carrying parameters",
            {
                "point": "execution",
                "mechanism": "invariant",
                "module": "opsdeck.governance.ledger",
                "parameters": {"field": "verification"},
            },
            "declares none",
        ),
        (
            "an invariant that does not say who keeps it",
            {"point": "execution", "mechanism": "invariant"},
            "must name the module",
        ),
        (
            "an instruction claiming a validator",
            {
                "point": "planning",
                "mechanism": "instruction",
                "validator": "value_in_set",
                "module": "opsdeck.planner.prompts",
            },
            "names no validator",
        ),
        (
            "an instruction carrying parameters",
            {
                "point": "planning",
                "mechanism": "instruction",
                "module": "opsdeck.planner.prompts",
                "parameters": {"field": "operation_key"},
            },
            "declares none",
        ),
    ],
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_each_mechanism_declares_only_what_it_can_be_held_to(
    tmp_path: Path, named: str, enforcement: dict, refused_for: str | None
) -> None:
    """A mechanism carries what keeps it and nothing that would suggest anything else."""
    document = _catalog_document(
        tmp_path,
        {
            "guardrail.under_test": {
                "title": f"A promise declared as {named}",
                "description": f"A promise this test declares as {named}.",
                "enforcement": enforcement,
            }
        },
    )

    if refused_for is None:
        catalog = GuardrailCatalog.load(document)
        assert catalog.require("guardrail.under_test").enforcement.mechanism == (
            enforcement["mechanism"]
        )
        return

    with pytest.raises(GuardrailError) as refused:
        GuardrailCatalog.load(document)

    assert refused_for in str(refused.value)


def test_a_check_declared_where_the_chain_runs_nothing_is_refused_at_load(
    tmp_path: Path,
) -> None:
    """Nothing runs checks while a plan is written, so a check declared there is a fiction."""
    document = _catalog_document(
        tmp_path,
        {
            "guardrail.checked_while_planning": {
                "title": "A check nothing would ever run",
                "description": "A rule declared as a check at a point the chain does not run.",
                "enforcement": {
                    "point": "planning",
                    "mechanism": "check",
                    "validator": "member_of_declared",
                    "module": "opsdeck.planner.planner",
                    "parameters": {"field": "operation_key", "declared": "declared_operations"},
                },
            }
        },
    )

    with pytest.raises(GuardrailError) as refused:
        GuardrailCatalog.load(document)

    assert "planning" in str(refused.value)
    assert "invariant" in str(refused.value)


def test_a_skill_naming_a_guardrail_the_catalog_does_not_define_is_an_error(
    catalog: GuardrailCatalog,
    write_skill: Callable[..., Path],
    runbook_frontmatter: Callable[..., dict],
    tmp_path: Path,
) -> None:
    write_skill(runbook_frontmatter(guardrails=[SECRETS, "guardrail.invented"]))
    skill = SkillRegistry([tmp_path / "skills"]).require("runbook.sample")

    with pytest.raises(GuardrailError) as refused:
        catalog.for_skill(skill)

    assert "guardrail.invented" in str(refused.value)


def test_a_check_naming_a_validator_the_runtime_does_not_register_is_refused_at_load(
    tmp_path: Path,
) -> None:
    document = _catalog_document(
        tmp_path,
        {
            "guardrail.imaginary": {
                "title": "An imaginary check",
                "description": "Something no code in this runtime knows how to run.",
                "enforcement": {
                    "point": "input",
                    "mechanism": "check",
                    "validator": "no_such_validator",
                },
            }
        },
    )

    with pytest.raises(GuardrailError) as refused:
        GuardrailCatalog.load(document)

    assert "no_such_validator" in str(refused.value)


@pytest.mark.parametrize(
    ("guardrail_id", "passes", "blocked", "context"),
    SEEDED_CHECKS,
    ids=[row[0] for row in SEEDED_CHECKS],
)
def test_each_seeded_check_lets_one_subject_through_and_refuses_the_other(
    catalog: GuardrailCatalog,
    guardrail_id: str,
    passes: str | dict,
    blocked: str | dict,
    context: dict,
) -> None:
    guardrail = catalog.require(guardrail_id)
    checks = dict(
        (found.id, check)
        for found, check in catalog.checks(guardrail.enforcement.point)
    )
    check = checks[guardrail_id]

    assert check(passes, context).ok
    refusal = check(blocked, context)

    assert not refusal.ok
    assert refusal.reason


def test_the_input_chain_refuses_a_credential_and_hands_back_a_redacted_copy(
    catalog: GuardrailCatalog,
) -> None:
    chain = GuardrailChain(catalog)

    refused = chain.apply_input(CARRIES_A_CREDENTIAL)
    allowed = chain.apply_input(RESTART_REQUEST)

    assert refused.ok is False
    assert refused.action == "blocked"
    assert refused.guardrail_id == SECRETS
    assert "AKIA5T7QZ2WJ4KDLMNOP" not in refused.text
    assert allowed.ok and allowed.action == "passed"
    assert allowed.text == RESTART_REQUEST


def test_a_chain_that_is_switched_off_lets_everything_through(
    catalog: GuardrailCatalog,
) -> None:
    assert GuardrailChain(catalog, enabled=False).apply_input(CARRIES_A_CREDENTIAL).ok


def test_instructions_are_the_prose_of_what_the_skills_declare(
    catalog: GuardrailCatalog,
    write_skill: Callable[..., Path],
    runbook_frontmatter: Callable[..., dict],
    tmp_path: Path,
) -> None:
    """Only instruction guardrails become prompt text; a check is kept out of it."""
    write_skill(runbook_frontmatter(guardrails=[IDENTIFIERS, VERIFIED_ONLY, SECRETS]))
    skill = SkillRegistry([tmp_path / "skills"]).require("runbook.sample")

    everything = catalog.instructions([skill])
    planning = catalog.instructions([skill], "planning")


    assert everything == [
        catalog.require(IDENTIFIERS).description,
        catalog.require(VERIFIED_ONLY).description,
    ]
    assert planning == [catalog.require(IDENTIFIERS).description]
    assert catalog.require(SECRETS).description not in everything
    assert catalog.instructions([]) == []


def test_a_conversation_carries_the_promises_as_a_system_message() -> None:
    conversation = [Message(role="user", content="restart it")]

    with_promises = guided(conversation, ["Never invent an identifier."])

    assert guided(conversation, []) == conversation
    assert with_promises[:-1] == conversation
    assert with_promises[-1].role == "system"
    assert "Never invent an identifier." in with_promises[-1].content
    assert instruction_block([]) == ""


def test_the_declared_promises_reach_the_planner_prompt(
    repo_root: Path, catalog_copy: Path, tmp_path: Path, catalog: GuardrailCatalog
) -> None:
    """A procedure that declares an instruction guardrail states it to the planner."""
    _declare_guardrails(catalog_copy, "runbook", "service_restart", [IDENTIFIERS])
    settings = _settings(repo_root, tmp_path / "state").model_copy(
        update={"skills_roots": [catalog_copy]}
    )
    recorder = _Recorder(
        ScriptedModelProvider([_route("new_task", "runbook.service_restart"), _restart_plan()])
    )
    engine = build_engine(settings, provider=recorder, audit_sink=MemorySink())

    engine.turn("promised", RESTART_REQUEST)

    stated = [block for block in recorder.systems() if IDENTIFIERS not in block]
    assert any(catalog.require(IDENTIFIERS).description in block for block in stated)


def test_the_reply_writer_is_handed_the_promises_of_the_turn(
    registry: SkillRegistry, resolver: CapabilityResolver, catalog: GuardrailCatalog
) -> None:
    recorder = _Recorder(
        ScriptedModelProvider([{"purpose": "the reply", "response": {"reply": "Done."}}])
    )
    builder = ReplyBuilder(Planner(recorder, registry, resolver), resolver, ReplyConfig())
    promise = catalog.require(VERIFIED_ONLY).description

    reply = builder.build(
        "outcomes",
        SessionState(session_id="s1"),
        [Outcome(kind="read", integration_id="integration.x", operation="x.read")],
        instructions=[promise],
    )

    assert reply == "Done."
    assert any(promise in block for block in recorder.systems())


def test_a_turn_the_input_guardrail_refuses_ends_there(
    repo_root: Path, tmp_path: Path
) -> None:
    """Nothing is classified, nothing is planned, nothing is written."""
    provider = ScriptedModelProvider([])
    engine = build_engine(
        _settings(repo_root, tmp_path / "state"),
        provider=provider,
        audit_sink=MemorySink(),
    )
    catalog = engine.guardrails

    result = engine.turn("refused", CARRIES_A_CREDENTIAL)

    kinds = [event.kind for event in result.events]
    assert [outcome.kind for outcome in result.outcomes] == ["denied"]
    assert result.outcomes[0].data["guardrail_id"] == SECRETS
    assert result.reply == catalog.require(SECRETS).description
    assert "guardrail" in kinds
    assert "route" not in kinds and "plan" not in kinds and "discovery" not in kinds
    assert provider.consumed() == 0
    assert engine.receipts("refused") == []
    assert engine.state("refused").pending_goal is None
    assert "AKIA5T7QZ2WJ4KDLMNOP" not in engine.state("refused").history[0].content


def test_the_output_chain_runs_over_the_reply_before_it_is_returned(
    repo_root: Path, tmp_path: Path
) -> None:
    """An output check that refuses replaces the reply with what the guardrail says."""
    document = _catalog_document(
        tmp_path,
        {
            "guardrail.nothing_leaves_unsaid": {
                "title": "The scope is stated in the same words every time",
                "description": "This installation states its scope from the catalog.",
                "enforcement": {
                    "point": "output",
                    "mechanism": "check",
                    "validator": "forbidden_pattern",
                    "module": "opsdeck.engine.reply",
                    "parameters": {
                        "patterns": ["(?i)not something I can do here"],
                        "replacement": "",
                        "reason": "the reply used wording this installation does not use",
                    },
                },
            }
        },
        beside=repo_root,
    )
    runtime = _runtime_config(
        repo_root,
        tmp_path,
        guardrails={"enabled": True, "catalog": str(document)},
    )
    settings = _settings(repo_root, tmp_path / "state").model_copy(
        update={"runtime_config": runtime}
    )
    engine = build_engine(
        settings,
        provider=ScriptedModelProvider([_route("off_scope")]),
        audit_sink=MemorySink(),
    )

    result = engine.turn("output", "book me a flight to Lisbon next Tuesday")

    acted = [event for event in result.events if event.kind == "guardrail"]
    assert result.reply == "This installation states its scope from the catalog."
    assert [event.data["point"] for event in acted] == ["output"]
    assert acted[0].data["guardrail_id"] == "guardrail.nothing_leaves_unsaid"


def test_guardrails_switched_off_leave_the_turn_alone(
    repo_root: Path, tmp_path: Path
) -> None:
    runtime = _runtime_config(
        repo_root,
        tmp_path,
        guardrails={"enabled": False, "catalog": "configs/guardrails.json"},
    )
    settings = _settings(repo_root, tmp_path / "state").model_copy(
        update={"runtime_config": runtime}
    )
    engine = build_engine(
        settings,
        provider=ScriptedModelProvider([_route("off_scope")]),
        audit_sink=MemorySink(),
    )

    result = engine.turn("open", CARRIES_A_CREDENTIAL)

    assert engine.guardrails.ids() == []
    assert [event.kind for event in result.events].count("route") == 1
    assert not [event for event in result.events if event.kind == "guardrail"]


EXAMPLE_DOMAINS = ("access", "ops")

PROCEDURES = (SkillType.runbook, SkillType.composite)


def _procedures(root: Path) -> list[Skill]:
    registry = SkillRegistry([root])
    return [skill for skill in registry.published() if skill.type in PROCEDURES]


@pytest.mark.parametrize("domain", EXAMPLE_DOMAINS)
def test_every_example_procedure_declares_promises_the_catalog_defines(
    catalog: GuardrailCatalog, repo_root: Path, domain: str
) -> None:
    """The shipped domains exercise the declaration, and claim nothing they do not keep.

    A procedure that writes claims the two promises about writing; a procedure
    that only reads claims neither, because a read-only procedure promising
    that a write is read back is promising something it never does.
    """
    root = repo_root / "examples" / domain / "skills"
    procedures = _procedures(root)
    assert procedures

    for procedure in procedures:
        # An identifier the catalog does not define raises here.
        declared = [guardrail.id for guardrail in catalog.for_skill(procedure)]
        writes = bool(procedure.frontmatter.capabilities.writes)
        assert DECLARED_OPERATIONS in declared
        assert IDENTIFIERS in declared
        assert (EXACT_PAYLOAD in declared) is writes, procedure.id
        assert (READ_BACK in declared) is writes, procedure.id


@pytest.mark.parametrize("domain", EXAMPLE_DOMAINS)
def test_a_hydrated_example_procedure_states_something_to_the_model(
    catalog: GuardrailCatalog, repo_root: Path, domain: str
) -> None:
    """What a turn hands a model is the prose of what the closure declares."""
    root = repo_root / "examples" / domain / "skills"
    registry = SkillRegistry([root])

    for procedure in _procedures(root):
        hydrated = registry.closure(procedure.id)
        skills = [
            hydrated.runbook,
            *hydrated.runbooks,
            *hydrated.entities,
            *hydrated.integrations,
        ]
        assert catalog.instructions(skills), procedure.id
        assert catalog.instructions(skills, "planning") == [
            catalog.require(IDENTIFIERS).description
        ]


def _deployment(repo_root: Path, tmp_path: Path, catalog: Path | None) -> Settings:
    """Settings for a deployment whose guardrail catalog is this file, or none."""
    runtime = _runtime_config(
        repo_root,
        tmp_path,
        guardrails={
            "enabled": True,
            "catalog": str(catalog if catalog is not None else tmp_path / "absent.json"),
        },
    )
    return _settings(repo_root, tmp_path / "state").model_copy(
        update={"runtime_config": runtime}
    )


def test_a_skill_declaring_an_identifier_no_catalog_defines_is_refused_at_publish(
    repo_root: Path,
    tmp_path: Path,
    write_skill: Callable[..., Path],
    runbook_frontmatter: Callable[..., dict],
) -> None:
    """The unknown identifier is caught where a catalog is checked, not mid-turn."""
    write_skill(runbook_frontmatter(guardrails=[IDENTIFIERS, "guardrail.invented"]))
    skills = SkillRegistry([tmp_path / "skills"]).skills()
    settings = _deployment(repo_root, tmp_path, repo_root / "configs" / "guardrails.json")

    issues = validate_all(skills, schema_dir=repo_root / "schemas", settings=settings)

    assert [(issue.code, issue.severity) for issue in issues] == [
        ("guardrail_unknown_id", "error")
    ]
    assert "guardrail.invented" in issues[0].message


def test_a_deployment_with_no_guardrail_catalog_defines_nothing(
    repo_root: Path,
    tmp_path: Path,
    write_skill: Callable[..., Path],
    runbook_frontmatter: Callable[..., dict],
) -> None:
    """Without a catalog nothing may be declared, and a skill declaring nothing holds up."""
    write_skill(runbook_frontmatter(id="runbook.quiet"))
    write_skill(runbook_frontmatter(id="runbook.loud", guardrails=[IDENTIFIERS]))
    registry = SkillRegistry([tmp_path / "skills"])
    settings = _deployment(repo_root, tmp_path, None)
    schemas = repo_root / "schemas"

    skills = registry.skills()
    quiet = validate(
        registry.require("runbook.quiet"), skills, schema_dir=schemas, settings=settings
    )
    loud = validate(
        registry.require("runbook.loud"), skills, schema_dir=schemas, settings=settings
    )

    assert quiet == []
    assert [issue.code for issue in loud] == ["guardrail_unknown_id"]


def test_an_engine_refuses_a_catalog_that_declares_an_identifier_nothing_defines(
    repo_root: Path,
    catalog_copy: Path,
    tmp_path: Path,
) -> None:
    """Nothing is served from a catalog whose promises the deployment cannot keep."""
    _declare_guardrails(catalog_copy, "runbook", "service_health", [IDENTIFIERS, "guardrail.gone"])
    settings = _settings(repo_root, tmp_path / "state").model_copy(
        update={"skills_roots": [catalog_copy]}
    )

    with pytest.raises(CatalogError) as refused:
        build_engine(settings, provider=ScriptedModelProvider([]), audit_sink=MemorySink())

    assert "guardrail.gone" in str(refused.value)
