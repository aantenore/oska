"""The HTTP surface, driven against a runtime built on the example catalog."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from opsdeck.api.app import create_app
from opsdeck.audit.sink import MemorySink
from opsdeck.config import Settings
from opsdeck.conformance import load_scenarios
from opsdeck.engine.factory import build_engine
from opsdeck.learning.proposals import ProposalStore
from opsdeck.planner.scripted import ScriptedModelProvider

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
def client(example_settings: Settings, script: list[dict], tmp_path: Path) -> TestClient:
    """A client over an application whose engine replays the scripted answers."""
    engine = build_engine(
        example_settings,
        provider=ScriptedModelProvider(script),
        audit_sink=MemorySink(),
    )
    app = create_app(
        example_settings,
        engine=engine,
        proposal_store=ProposalStore(tmp_path / "proposals"),
    )
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
    assert body["state"] == "submitted"
    assert body["history"][-1]["by"] == "a reviewer"


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
