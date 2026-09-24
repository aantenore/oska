"""The identity a write is bound to, and the three places it is enforced.

The rules under test are one rule seen from three sides: the value comes from
the channel and never from the conversation. The resolver says what the value
is, the executor refuses a plan that supplies it and refuses to run at all
without one, and the HTTP surface takes it from the proxy and holds one session
to one identity.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from oska.api.app import create_app
from oska.api.routes import ATTRIBUTES_HEADER, SUBJECT_HEADER
from oska.audit.sink import MemorySink
from oska.catalog.registry import SkillRegistry
from oska.catalog.validator import validate
from oska.config import Settings
from oska.contracts.common import TurnResult
from oska.engine.factory import build_engine
from oska.governance.identity import (
    IdentityError,
    Principal,
    PrincipalMismatch,
    refused_fields,
    requires_principal,
    resolve_inputs,
)
from oska.planner.scripted import ScriptedModelProvider

GRANT = "runbook.access_grant"
CHECK = "runbook.access_check"
SESSION = "identity-session"

ROUTE_STEP = {
    "purpose": "router classification",
    "response": {
        "kind": "new_task",
        "runbook_id": GRANT,
        "confidence": 0.9,
        "language": "en",
    },
}
REPLY_STEP = {"purpose": "reply writing", "response": {"reply": "Reported."}}
REQUEST = "I am on the support rota from this week, put me in the support desk group"


def _plan(payload: dict) -> dict:
    """A plan for the grant whose write carries exactly this payload."""
    return {
        "purpose": "plan",
        "response": {
            "runbook_id": GRANT,
            "operations": [
                {
                    "integration_id": "integration.directory",
                    "operation": "group.get",
                    "payload": {"group": "support-desk"},
                    "write": False,
                    "purpose": "read the group",
                },
                {
                    "integration_id": "integration.access_control",
                    "operation": "membership.grant",
                    "payload": payload,
                    "write": True,
                    "purpose": "put the account into the group",
                },
            ],
            "missing": [],
            "reply_intent": "report the membership",
        },
    }


@pytest.fixture
def access_settings(repo_root: Path, tmp_path: Path) -> Settings:
    """Settings over the access catalog, with all state under a temporary directory."""
    root = repo_root / "examples" / "access"
    return Settings(
        skills_roots=[root / "skills"],
        runtime_config=repo_root / "configs" / "runtime.json",
        model_config_path=repo_root / "configs" / "model.json",
        connectors_config=root / "configs" / "connectors.json",
        schemas_dir=repo_root / "schemas",
        state_dir=tmp_path / "state",
        provider="scripted",
    )


@pytest.fixture
def access_registry(repo_root: Path) -> SkillRegistry:
    """Registry over the access catalog."""
    return SkillRegistry([repo_root / "examples" / "access" / "skills"])


@pytest.fixture
def signed_in() -> Principal:
    """The identity the channel authenticated for these sessions."""
    return Principal("tnovak", {"department": "support"}, True)


def test_a_declared_field_is_filled_from_the_identity(access_registry: SkillRegistry) -> None:
    skill = access_registry.get(GRANT)
    assert skill is not None
    resolved = resolve_inputs(skill, Principal("tnovak", {"department": "support"}, True))
    assert resolved == {"username": "tnovak"}


def test_an_attribute_other_than_the_subject_can_be_named(
    access_registry: SkillRegistry,
) -> None:
    skill = access_registry.get(GRANT)
    assert skill is not None
    skill.frontmatter.principal_inputs["username"] = "department"
    assert resolve_inputs(skill, Principal("tnovak", {"department": "support"}, True)) == {
        "username": "support"
    }


def test_a_procedure_that_declares_nothing_resolves_nothing(
    access_registry: SkillRegistry,
) -> None:
    skill = access_registry.get(CHECK)
    assert skill is not None
    assert resolve_inputs(skill, None) == {}
    assert not requires_principal([skill])


def test_an_absent_attribute_is_an_identity_error(access_registry: SkillRegistry) -> None:
    skill = access_registry.get(GRANT)
    assert skill is not None
    skill.frontmatter.principal_inputs["username"] = "employee_number"
    with pytest.raises(IdentityError, match="employee_number"):
        resolve_inputs(skill, Principal("tnovak", {}, True))


def test_an_unauthenticated_identity_is_no_identity(access_registry: SkillRegistry) -> None:
    skill = access_registry.get(GRANT)
    assert skill is not None
    with pytest.raises(IdentityError, match="no authenticated identity"):
        resolve_inputs(skill, Principal("tnovak", {}, False))
    with pytest.raises(IdentityError, match="no authenticated identity"):
        resolve_inputs(skill, None)


def test_the_declared_field_is_recognized_in_a_payload(access_registry: SkillRegistry) -> None:
    skill = access_registry.get(GRANT)
    assert skill is not None
    assert refused_fields(skill, {"group": "support-desk"}) == []
    assert refused_fields(skill, {"group": "support-desk", "username": "rsilva"}) == ["username"]


def test_the_runtime_fills_the_field_the_plan_leaves_out(
    access_settings: Settings,
    signed_in: Principal,
) -> None:
    provider = ScriptedModelProvider([ROUTE_STEP, _plan({"group": "support-desk"})])
    engine = build_engine(access_settings, provider=provider, audit_sink=MemorySink())
    result = engine.turn(SESSION, REQUEST, signed_in)
    pending = result.pending
    assert pending is not None and pending.approval is not None
    assert pending.approval.exact_payload == {"group": "support-desk", "username": "tnovak"}
    assert engine.state(SESSION).principal["subject"] == "tnovak"


def test_a_plan_that_supplies_the_field_is_refused(
    access_settings: Settings,
    signed_in: Principal,
) -> None:
    provider = ScriptedModelProvider(
        [ROUTE_STEP, _plan({"group": "support-desk", "username": "rsilva"}), REPLY_STEP]
    )
    engine = build_engine(access_settings, provider=provider, audit_sink=MemorySink())
    result = engine.turn(SESSION, REQUEST, signed_in)
    assert [outcome.kind for outcome in result.outcomes] == ["invalid_plan"]
    assert "takes username from the authenticated session" in str(result.outcomes[0].data)
    assert result.pending is None
    assert engine.receipts(SESSION) == []


def test_a_session_with_nobody_signed_in_is_refused_before_it_plans(
    access_settings: Settings,
) -> None:
    provider = ScriptedModelProvider([ROUTE_STEP, REPLY_STEP])
    engine = build_engine(access_settings, provider=provider, audit_sink=MemorySink())
    result = engine.turn(SESSION, REQUEST)
    assert [outcome.kind for outcome in result.outcomes] == ["invalid_plan"]
    assert "nobody is signed in" in str(result.outcomes[0].data)
    # The router was asked and the planner never was: the value is not asked for.
    assert provider.consumed() == 2
    assert result.pending is None


def test_a_session_is_held_to_the_identity_it_was_opened_with(
    example_settings: Settings,
    signed_in: Principal,
) -> None:
    engine = build_engine(example_settings, audit_sink=MemorySink())
    engine.turn(SESSION, "hello", signed_in)
    assert engine.state(SESSION).principal == {
        "subject": "tnovak",
        "attributes": {"department": "support"},
        "authenticated": True,
    }
    with pytest.raises(PrincipalMismatch, match="arrives as rsilva"):
        engine.turn(SESSION, "hello", Principal("rsilva", {}, True))
    with pytest.raises(PrincipalMismatch, match="carries no identity"):
        engine.turn(SESSION, "hello")
    assert engine.state(SESSION).turn_count == 1


def test_a_principal_input_naming_no_field_is_reported(
    access_registry: SkillRegistry,
    schemas_dir: Path,
) -> None:
    skill = access_registry.get(GRANT)
    assert skill is not None
    skill.frontmatter.principal_inputs["employee_number"] = "subject"
    codes = [
        issue.code
        for issue in validate(
            skill,
            access_registry.skills(),
            schema_dir=schemas_dir,
            guardrail_ids=frozenset(skill.frontmatter.guardrails),
        )
    ]
    assert codes == ["principal_input_not_declared"]


def test_a_principal_input_that_shadows_a_human_input_is_reported(
    access_registry: SkillRegistry,
    schemas_dir: Path,
) -> None:
    skill = access_registry.get(GRANT)
    assert skill is not None
    # The contract refuses this pairing; the catalog check is what catches a
    # frontmatter that reached the catalog without passing through it.
    skill.frontmatter.human_inputs.append("username")
    codes = [
        issue.code
        for issue in validate(
            skill,
            access_registry.skills(),
            schema_dir=schemas_dir,
            guardrail_ids=frozenset(skill.frontmatter.guardrails),
        )
    ]
    assert codes == ["principal_input_shadows_human_input"]


class _RecordingEngine:
    """An engine that records the identity it was handed and answers nothing else."""

    def __init__(self, raises: Exception | None = None) -> None:
        self.seen: list[Principal | None] = []
        self._raises = raises

    def turn(self, session_id: str, text: str, principal: Principal | None = None) -> TurnResult:
        """Record the identity of this turn, and answer."""
        if self._raises is not None:
            raise self._raises
        self.seen.append(principal)
        return TurnResult(reply=f"{session_id}: {text}")


def test_the_identity_of_a_turn_comes_from_the_proxy_headers(
    example_settings: Settings,
) -> None:
    engine = _RecordingEngine()
    client = TestClient(create_app(example_settings, engine=engine))
    response = client.post(
        f"/sessions/{SESSION}/turns",
        json={"text": "hello"},
        headers={
            SUBJECT_HEADER: "tnovak",
            ATTRIBUTES_HEADER: '{"department": "support"}',
        },
    )
    assert response.status_code == 200
    assert engine.seen == [Principal("tnovak", {"department": "support"}, True)]


def test_a_turn_without_the_header_carries_no_identity(example_settings: Settings) -> None:
    engine = _RecordingEngine()
    client = TestClient(create_app(example_settings, engine=engine))
    assert client.post(f"/sessions/{SESSION}/turns", json={"text": "hello"}).status_code == 200
    assert engine.seen == [None]


def test_attributes_that_are_not_an_object_are_refused(example_settings: Settings) -> None:
    engine = _RecordingEngine()
    client = TestClient(create_app(example_settings, engine=engine))
    response = client.post(
        f"/sessions/{SESSION}/turns",
        json={"text": "hello"},
        headers={SUBJECT_HEADER: "tnovak", ATTRIBUTES_HEADER: "not json"},
    )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_principal"
    assert engine.seen == []


def test_a_real_turn_over_http_records_the_identity_on_the_session(
    example_settings: Settings,
) -> None:
    engine = build_engine(example_settings, audit_sink=MemorySink())
    client = TestClient(create_app(example_settings, engine=engine))
    first = client.post(
        f"/sessions/{SESSION}/turns",
        json={"text": "hello"},
        headers={SUBJECT_HEADER: "tnovak"},
    )
    assert first.status_code == 200
    assert engine.state(SESSION).principal["subject"] == "tnovak"
    second = client.post(
        f"/sessions/{SESSION}/turns",
        json={"text": "hello"},
        headers={SUBJECT_HEADER: "rsilva"},
    )
    assert second.status_code == 409
    assert engine.state(SESSION).turn_count == 1


def test_a_turn_under_a_second_identity_is_refused_by_the_surface(
    example_settings: Settings,
) -> None:
    engine = _RecordingEngine(PrincipalMismatch("this session was opened by tnovak"))
    client = TestClient(create_app(example_settings, engine=engine))
    response = client.post(
        f"/sessions/{SESSION}/turns",
        json={"text": "hello"},
        headers={SUBJECT_HEADER: "rsilva"},
    )
    assert response.status_code == 409
    assert response.json()["error"] == "principal_mismatch"
