"""The HTTP surface, driven against a runtime built on the example catalog."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from oska.api.app import create_app
from oska.audit.sink import MemorySink
from oska.config import Settings, load_runtime_config
from oska.conformance import load_scenarios
from oska.contracts.proposal import SkillProposal
from oska.engine.factory import build_engine
from oska.learning.changes import ChangeRequestState, ChangeRequestStore, store_for
from oska.learning.proposals import ProposalStore
from oska.planner.scripted import ScriptedModelProvider
from oska.platform import Platform

SESSION = "api-session"

# The turns the scripted provider answers: the restart request and its approval,
# taken from the scenario that records them, then one answer for the draft the
# proposal endpoint asks for.
DRAFT_STEP = {
    "purpose": "proposal draft",
    "response": {
        "description": "Restarts a service and reads its state back, as one session did it.",
        "triggers": ["restart that service", "bounce the service", "run the restart procedure"],
        "action": "restart a service",
        "purpose": "Restarts one service and confirms the result by reading its state back.",
        "preconditions": "- The service name is known.",
        "steps": "1. Call `service.restart` with the service name.\n2. Read the state back.",
        "verification": "The status read back names the service as running.",
        "rollback": "None was recorded.",
    },
}


@pytest.fixture
def script(repo_root: Path) -> list[dict]:
    """The first three steps of the restart scenario, plus a draft answer."""
    scenarios = {scenario.name: scenario for scenario in load_scenarios(_scenario_dir(repo_root))}
    return [*scenarios["02_restart_staging"].provider_script[:3], DRAFT_STEP]


@pytest.fixture
def changes(example_settings: Settings) -> ChangeRequestStore:
    """The change request store the application writes its publications to."""
    return store_for(example_settings)


@pytest.fixture
def app(example_settings: Settings, script: list[dict], tmp_path: Path) -> FastAPI:
    """An application whose engine replays the scripted answers."""
    engine = build_engine(
        example_settings,
        provider=ScriptedModelProvider(script),
        audit_sink=MemorySink(),
    )
    return create_app(
        example_settings,
        engine=engine,
        proposal_store=ProposalStore(tmp_path / "proposals"),
    )


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    """A client over that application."""
    return TestClient(app)


def test_turn_sequence_records_a_verified_receipt(client: TestClient) -> None:
    first = client.post(f"/sessions/{SESSION}/turns", json={"text": _request_text()})
    assert first.status_code == 200
    body = first.json()
    assert body["pending"]["kind"] == "approval"
    assert body["pending"]["approval"]["operation"] == "service.restart"
    assert [outcome["kind"] for outcome in body["outcomes"]] == ["read", "approval_pending"]

    second = client.post(f"/sessions/{SESSION}/turns", json={"text": "yes"})
    assert second.status_code == 200
    assert [outcome["kind"] for outcome in second.json()["outcomes"]] == ["receipt"]

    receipts = client.get(f"/sessions/{SESSION}/receipts")
    assert receipts.status_code == 200
    recorded = receipts.json()
    assert len(recorded) == 1
    assert recorded[0]["operation"] == "service.restart"
    assert recorded[0]["verification"] == "verified"


def test_turn_without_text_is_rejected(client: TestClient) -> None:
    response = client.post(f"/sessions/{SESSION}/turns", json={})
    assert response.status_code == 422
    assert response.json()["error"] == "invalid_request"


def test_skills_endpoints(client: TestClient) -> None:
    listing = client.get("/skills")
    assert listing.status_code == 200
    entries = listing.json()
    identifiers = [entry["id"] for entry in entries]
    assert "runbook.service_restart" in identifiers
    restart = next(entry for entry in entries if entry["id"] == "runbook.service_restart")
    assert restart["type"] == "runbook"
    assert restart["status"] == "published"
    assert restart["version"] >= 1
    assert restart["description"]

    detail = client.get("/skills/runbook.service_restart")
    assert detail.status_code == 200
    body = detail.json()
    assert body["frontmatter"]["id"] == "runbook.service_restart"
    assert body["sections"]["verification"]

    missing = client.get("/skills/runbook.nothing_like_this")
    assert missing.status_code == 404
    assert missing.json()["error"] == "unknown_skill"


def test_capabilities_come_from_the_catalog(client: TestClient) -> None:
    response = client.get("/capabilities")
    assert response.status_code == 200
    body = response.json()
    assert body["declared_scope"].startswith("I can ")
    phrases = [process["action"] for process in body["processes"]]
    assert phrases
    for phrase in phrases:
        assert phrase in body["declared_scope"]


def test_health_reports_the_catalog(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["skills"] > 0
    assert body["fingerprint"]


def test_proposal_is_drafted_and_submitted(client: TestClient) -> None:
    _run_the_restart(client)

    drafted = client.post(f"/proposals/from-session/{SESSION}")
    assert drafted.status_code == 200
    proposal = drafted.json()
    assert proposal["state"] == "draft"
    assert proposal["source_session_id"] == SESSION
    assert "service.restart" in proposal["skill_markdown"]
    proposal_id = proposal["proposal_id"]

    assert [entry["proposal_id"] for entry in client.get("/proposals").json()] == [proposal_id]
    assert client.get(f"/proposals/{proposal_id}").json()["state"] == "draft"

    moved = client.post(
        f"/proposals/{proposal_id}/transition",
        json={"to": "submitted", "by": "a reviewer", "note": "worth keeping"},
    )
    assert moved.status_code == 200
    body = moved.json()
    assert body["proposal"]["state"] == "submitted"
    assert body["proposal"]["history"][-1]["by"] == "a reviewer"
    assert body["change_request"] is None


def test_publication_answers_with_a_change_request(
    client: TestClient, changes: ChangeRequestStore, skills_root: Path
) -> None:
    """Publishing over HTTP composes a package; the catalog it serves is untouched."""
    _run_the_restart(client)
    proposal_id = client.post(f"/proposals/from-session/{SESSION}").json()["proposal_id"]
    for state in ("submitted", "approved"):
        client.post(
            f"/proposals/{proposal_id}/transition", json={"to": state, "by": "a reviewer"}
        )

    published = client.post(
        f"/proposals/{proposal_id}/transition", json={"to": "published", "by": "a reviewer"}
    )

    assert published.status_code == 200
    body = published.json()
    assert body["proposal"]["state"] == "published"
    request = body["change_request"]
    assert request is not None
    assert request["proposal_id"] == proposal_id
    assert request["state"] == ChangeRequestState.open.value
    assert request["version"] == 2
    assert request["actor"] == "a reviewer"

    listing = client.get("/changes")
    assert listing.status_code == 200
    assert [held["request_id"] for held in listing.json()] == [request["request_id"]]
    one = client.get(f"/changes/{request['request_id']}")
    assert one.status_code == 200
    assert one.json()["skill_id"] == request["skill_id"]

    package = changes.package(request["request_id"])
    assert (package / "skill.md").is_file()
    assert not (skills_root / request["skill_id"].split(".")[1]).exists()
    assert not (skills_root / "runbook" / "service_restart" / "v2").exists()


def test_an_unknown_change_request_is_not_found(client: TestClient) -> None:
    response = client.get("/changes/nothing-like-this")
    assert response.status_code == 404
    assert response.json()["error"] == "unknown_change_request"


def test_no_route_applies_a_change_request(app: FastAPI, client: TestClient) -> None:
    """The refusal is structural: there is no path that could apply one.

    Every route the application declares is listed and the ones that write are
    counted. A change request is only ever read here, so `apply` and `reject`
    have no address at all, whatever verb is tried against the obvious ones.
    """
    declared = app.openapi()["paths"]
    offered = {
        (path, verb.upper()) for path, operations in declared.items() for verb in operations
    }
    change_routes = {(path, verb) for path, verb in offered if path.startswith("/changes")}
    assert change_routes == {("/changes", "GET"), ("/changes/{request_id}", "GET")}
    assert not any("apply" in path or "reject" in path for path, _ in offered)
    assert not any(verb != "GET" for path, verb in change_routes)

    for path in ("/changes/any-id/apply", "/changes/apply", "/changes/any-id/reject"):
        assert client.post(path, json={"by": "someone", "into": "/tmp"}).status_code in (404, 405)
    for verb in (client.post, client.put, client.patch, client.delete):
        assert verb("/changes/any-id").status_code in (404, 405)


def test_illegal_transition_is_refused(client: TestClient) -> None:
    _run_the_restart(client)
    proposal_id = client.post(f"/proposals/from-session/{SESSION}").json()["proposal_id"]

    response = client.post(
        f"/proposals/{proposal_id}/transition", json={"to": "published", "by": "a reviewer"}
    )
    assert response.status_code == 409
    assert response.json()["error"] == "illegal_transition"
    assert "draft" in response.json()["detail"]
    assert client.get(f"/proposals/{proposal_id}").json()["state"] == "draft"


def test_unknown_state_is_refused(client: TestClient) -> None:
    _run_the_restart(client)
    proposal_id = client.post(f"/proposals/from-session/{SESSION}").json()["proposal_id"]

    response = client.post(
        f"/proposals/{proposal_id}/transition", json={"to": "elsewhere", "by": "a reviewer"}
    )
    assert response.status_code == 422
    assert response.json()["error"] == "unknown_state"


def test_unknown_proposal_is_not_found(client: TestClient) -> None:
    response = client.get("/proposals/nothing-like-this")
    assert response.status_code == 404
    assert response.json()["error"] == "unknown_proposal"


def _run_the_restart(client: TestClient) -> None:
    client.post(f"/sessions/{SESSION}/turns", json={"text": _request_text()})
    client.post(f"/sessions/{SESSION}/turns", json={"text": "yes"})


def _request_text() -> str:
    return "the search service in staging needs a restart, it is wedged again"


def _scenario_dir(repo_root: Path) -> Path:
    return repo_root / "examples" / "ops" / "scenarios"


def test_http_turns_run_per_domain_and_a_session_stays_in_its_domain(
    example_platform: Platform, platform_settings: Settings
) -> None:
    client = TestClient(create_app(platform_settings, platform=example_platform))

    domains = client.get("/domains").json()
    here = client.post("/domains/access/sessions/web/turns", json={"text": "hello"})
    there = client.post("/domains/operations/sessions/web/turns", json={"text": "hello"})
    legacy = client.post("/sessions/other/turns", json={"text": "hello"})
    scope = client.get("/domains/access/capabilities").json()["declared_scope"]

    assert [(entry["name"], entry["default"]) for entry in domains] == [
        ("operations", True),
        ("access", False),
    ]
    assert domains[1]["host"]["name"] == "the access desk"
    assert here.status_code == 200 and "the access desk" in here.json()["reply"]
    assert there.status_code == 403 and there.json()["error"] == "session_refused"
    assert "access" not in there.text
    assert "the operations desk" in legacy.json()["reply"]
    assert scope.startswith("I am the access desk.")
    assert client.get("/domains/billing/capabilities").status_code == 404
    unknown = client.post(
        "/domains/access/sessions/t/turns",
        json={"text": "hello"},
        headers={"X-Principal-Subject": "ana", "X-Principal-Attributes": '{"tenant": "acme"}'},
    )
    assert unknown.status_code == 404 and unknown.json()["error"] == "unknown_tenant"


def test_the_tenant_comes_from_the_principal_before_the_header(
    examples: Path, tmp_path: Path, platform_settings: Settings
) -> None:
    document = yaml.safe_load((examples / "platform.yaml").read_text(encoding="utf-8"))
    for domain in document["domains"]:
        domain["tenants"] = ["default", "acme"]
    (tmp_path / "platform.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")
    for pack in ("ops", "access"):
        (tmp_path / pack).symlink_to(examples / pack)
    held = Platform.from_file(tmp_path / "platform.yaml", platform_settings)
    client = TestClient(create_app(platform_settings, platform=held))
    headers = {
        "X-Principal-Subject": "ana",
        "X-Principal-Attributes": json.dumps({"tenant": "acme"}),
        "X-Tenant": "default",
    }

    answered = client.post("/domains/access/sessions/p/turns", json={"text": "hi"}, headers=headers)

    assert answered.status_code == 200
    assert (Path(platform_settings.state_dir) / "acme" / "access" / "sessions.sqlite").is_file()
    assert not (Path(platform_settings.state_dir) / "default" / "access").exists()


def test_a_single_catalog_is_a_platform_of_one_domain(example_settings: Settings) -> None:
    client = TestClient(create_app(example_settings.model_copy(update={"provider": "none"})))

    assert [entry["name"] for entry in client.get("/domains").json()] == ["default"]
    answered = client.post("/domains/default/sessions/s/turns", json={"text": "hello"})
    assert answered.status_code == 200
    assert client.get("/domains/other/capabilities").status_code == 404


# Drafting is bounded: a session must hold something, and the model call is a turn's.


def test_drafting_from_a_session_with_nothing_in_it_is_not_found(
    app: FastAPI, client: TestClient
) -> None:
    unknown = client.post("/proposals/from-session/never-opened")
    client.post(f"/sessions/{SESSION}/turns", json={"text": _request_text()})
    other = client.post("/proposals/from-session/also-never-opened")

    assert unknown.status_code == other.status_code == 404
    assert unknown.json() == other.json()
    assert unknown.json()["error"] == "nothing_to_draft"
    assert "never-opened" not in unknown.text
    assert app.state.services.proposals.list() == []


def test_the_draft_s_model_call_is_held_to_the_turn_budget(
    example_settings: Settings, tmp_path: Path
) -> None:
    runtime = load_runtime_config(settings=example_settings)
    runtime = runtime.model_copy(
        update={"turn": runtime.turn.model_copy(update={"max_model_calls": 0})}
    )
    provider = ScriptedModelProvider([DRAFT_STEP])
    engine = build_engine(
        example_settings, provider=provider, runtime=runtime, audit_sink=MemorySink()
    )
    client = TestClient(
        create_app(
            example_settings, engine=engine, proposal_store=ProposalStore(tmp_path / "proposals")
        )
    )
    assert client.post(f"/sessions/{SESSION}/turns", json={"text": "hello"}).status_code == 200

    drafted = client.post(f"/proposals/from-session/{SESSION}")

    assert drafted.status_code == 200, drafted.text
    assert provider.consumed() == 0
    assert DRAFT_STEP["response"]["description"] not in drafted.json()["skill_markdown"]


# A platform's non-default domain is reachable for receipts and drafts.


def test_a_non_default_domain_s_session_has_receipts_and_drafts(
    example_platform: Platform, platform_settings: Settings
) -> None:
    client = TestClient(create_app(platform_settings, platform=example_platform))
    ana = {"X-Principal-Subject": "ana"}
    opened = client.post("/domains/access/sessions/web/turns", json={"text": "hello"}, headers=ana)
    assert opened.status_code == 200

    receipts = client.get("/domains/access/sessions/web/receipts", headers=ana)
    drafted = client.post("/domains/access/proposals/from-session/web", headers=ana)
    elsewhere = client.post("/domains/access/proposals/from-session/web", headers={
        "X-Principal-Subject": "bob"
    })

    assert receipts.status_code == 200 and receipts.json() == []
    assert drafted.status_code == 200, drafted.text
    assert drafted.json()["source_session_id"] == "web"
    assert elsewhere.status_code == 403 and elsewhere.json()["error"] == "session_refused"
    access = example_platform.services("access", "default").proposals.list()
    assert [held.proposal_id for held in access] == [drafted.json()["proposal_id"]]
    assert client.post("/proposals/from-session/web", headers=ana).status_code == 403


def test_a_transition_reads_the_tenant_s_languages(
    examples: Path, tmp_path: Path, platform_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = yaml.safe_load((examples / "platform.yaml").read_text(encoding="utf-8"))
    for domain in document["domains"]:
        domain["tenants"] = ["default", "acme"]
    (tmp_path / "platform.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")
    for pack in ("ops", "access"):
        (tmp_path / pack).symlink_to(examples / pack)
    overlay = tmp_path / "tenants" / "acme"
    overlay.mkdir(parents=True)
    (overlay / "runtime.json").write_text(
        json.dumps({"discovery": {"languages": ["en"]}}), encoding="utf-8"
    )
    held = Platform.from_file(tmp_path / "platform.yaml", platform_settings)
    held.services(None, "acme").proposals.save(
        SkillProposal(proposal_id="p-1", source_session_id="s", skill_markdown="---\n---\n")
    )
    seen: dict = {}

    def recorded(*args: object, **kwargs: object) -> SkillProposal:
        seen.update(kwargs)
        return held.services(None, "acme").proposals.get("p-1")

    monkeypatch.setattr("oska.api.routes.transition", recorded)
    client = TestClient(create_app(platform_settings, platform=held))

    moved = client.post(
        "/proposals/p-1/transition",
        json={"to": "submitted"},
        headers={
            "X-Principal-Subject": "rita",
            "X-Principal-Attributes": json.dumps({"tenant": "acme", "roles": ["reviewer"]}),
        },
    )

    assert moved.status_code == 200, moved.text
    assert seen["languages"] == ["en"]
    assert held.runtime.discovery.languages == ["en", "it"]


def test_the_runtime_configuration_is_read_once_per_application(
    example_settings: Settings, tmp_path: Path
) -> None:
    runtime_path = tmp_path / "runtime.json"
    runtime_path.write_text(
        example_settings.runtime_config.read_text(encoding="utf-8"), encoding="utf-8"
    )
    settings = example_settings.model_copy(
        update={"runtime_config": runtime_path, "provider": "none"}
    )
    client = TestClient(create_app(settings))
    assert client.get("/skills").status_code == 200

    document = json.loads(runtime_path.read_text(encoding="utf-8"))
    document["identity"]["required"] = True
    runtime_path.write_text(json.dumps(document), encoding="utf-8")

    assert client.get("/skills").status_code == 200
    runtime_path.unlink()
    assert client.get("/capabilities").status_code == 200
