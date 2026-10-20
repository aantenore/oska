"""Who may act in a session, and who is recorded as having moved a proposal.

Each channel call is held to the identity it carries: a session call to the
session its token was issued for and to the subject the session was opened by,
a transition to the principal rather than a name in its body, and a tenant to
the principal or to a header only the proxy sets. A refusal about a session
says nothing about that session.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from oska.api.app import create_app
from oska.api.identity import ATTRIBUTES_HEADER, SUBJECT_HEADER
from oska.audit.sink import MemorySink
from oska.cli.main import app as cli_app
from oska.config import IdentityConfig, Settings
from oska.conformance import load_scenarios
from oska.contracts.common import Message
from oska.contracts.proposal import ActorSource, QualityReport, SkillProposal
from oska.contracts.session import PendingApproval, PendingGoal, SessionState
from oska.engine.binding import bind
from oska.engine.factory import build_engine
from oska.governance.identity import Principal, PrincipalMismatch, mint_token
from oska.learning.changes import ChangeRequestError, ChangeRequestStore, new_request
from oska.learning.proposals import ProposalStore
from oska.learning.publishers import GitPublisher
from oska.planner.scripted import ScriptedModelProvider
from oska.platform import Platform

KEY_ENV = "CHANNEL_IDENTITY_TEST_KEY"
KEY = "k" * 32
FAR = 4_000_000_000
RESTART = "the search service in staging needs a restart, it is wedged again"
MARKDOWN = "---\nid: runbook.sample\n---\n\n## Steps\n\n1. Do it.\n"
POLICY = {"skill_id": "runbook.sample", "default": "deny", "rules": []}
REVIEWER = "reviewer"
REFUSED_REVIEW = {"error": "reviewer_required", "detail": "this call needs the reviewer role"}


def _runtime(repo_root: Path, target: Path, identity: dict) -> Path:
    """A copy of the shipped runtime configuration with this identity block."""
    document = json.loads((repo_root / "configs" / "runtime.json").read_text(encoding="utf-8"))
    document["identity"] = identity
    document["guardrails"]["catalog"] = str(repo_root / "configs" / "guardrails.json")
    target.write_text(json.dumps(document), encoding="utf-8")
    return target


def _settings(example_settings: Settings, repo_root: Path, tmp_path: Path, **identity) -> Settings:
    runtime = _runtime(repo_root, tmp_path / "runtime.json", identity)
    return example_settings.model_copy(update={"runtime_config": runtime, "provider": "none"})


def _bearer(subject: str, session_id: str, roles: list[str] | None = None) -> dict[str, str]:
    claims: dict = {"sub": subject, "sid": session_id, "exp": FAR}
    if roles is not None:
        claims["roles"] = roles
    token = mint_token(claims, KEY)
    return {"Authorization": f"Bearer {token}"}


def _proxied(subject: str, roles: list[str] | None = None) -> dict[str, str]:
    headers = {SUBJECT_HEADER: subject}
    if roles is not None:
        headers[ATTRIBUTES_HEADER] = json.dumps({"roles": roles})
    return headers


@pytest.fixture
def token_app(
    example_settings: Settings, repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> FastAPI:
    """Token identity, with `required` left to its default for the mode."""
    monkeypatch.setenv(KEY_ENV, KEY)
    return create_app(
        _settings(example_settings, repo_root, tmp_path, mode="token", key_env=KEY_ENV)
    )


@pytest.fixture
def proxy_app(example_settings: Settings, repo_root: Path, tmp_path: Path) -> FastAPI:
    """Proxy identity, not required: the single-user default."""
    return create_app(_settings(example_settings, repo_root, tmp_path, mode="proxy"))


def _alice_opens_victim(client: TestClient) -> None:
    for text in (RESTART, "yes"):
        answered = client.post(
            "/sessions/victim/turns", json={"text": text}, headers=_bearer("alice", "victim")
        )
        assert answered.status_code == 200


# The review's probe, finding by finding.


def test_an_anonymous_call_cannot_read_a_session_s_receipts_in_token_mode(
    token_app: FastAPI,
) -> None:
    client = TestClient(token_app)
    _alice_opens_victim(client)

    anonymous = client.get("/sessions/victim/receipts")

    assert anonymous.status_code == 401
    assert anonymous.json()["error"] == "identity_required"
    own = client.get("/sessions/victim/receipts", headers=_bearer("alice", "victim"))
    assert own.status_code == 200


def test_a_token_for_one_session_cannot_draft_from_another(token_app: FastAPI) -> None:
    client = TestClient(token_app)
    _alice_opens_victim(client)

    drafted = client.post("/proposals/from-session/victim", headers=_bearer("eve", "mine"))

    assert drafted.status_code == 403
    assert drafted.json()["error"] == "session_refused"
    assert token_app.state.services.proposals.list() == []


def test_a_token_for_one_session_cannot_read_another_s_receipts(token_app: FastAPI) -> None:
    client = TestClient(token_app)
    _alice_opens_victim(client)

    read = client.get("/sessions/victim/receipts", headers=_bearer("eve", "mine"))

    assert read.status_code == 403
    assert "alice" not in read.text and "victim" not in read.text


def test_an_anonymous_turn_is_refused_before_it_reaches_a_session(token_app: FastAPI) -> None:
    client = TestClient(token_app)
    _alice_opens_victim(client)

    anonymous = client.post("/sessions/victim/turns", json={"text": "hi"})

    assert anonymous.status_code == 401
    assert "alice" not in anonymous.text


def test_an_anonymous_transition_is_refused_in_token_mode(token_app: FastAPI) -> None:
    client = TestClient(token_app)
    _hold_proposal(token_app)

    moved = client.post("/proposals/p-1/transition", json={"to": "submitted", "by": "alice"})

    assert moved.status_code == 401
    assert _proposal(token_app).state.value == "draft"


def test_the_transition_actor_is_the_principal_and_not_the_body(token_app: FastAPI) -> None:
    client = TestClient(token_app)
    _hold_proposal(token_app)

    moved = client.post(
        "/proposals/p-1/transition",
        json={"to": "submitted", "by": "somebody else"},
        headers=_bearer("alice", "any", roles=[REVIEWER]),
    )

    assert moved.status_code == 200, moved.text
    entry = moved.json()["proposal"]["history"][-1]
    assert entry["by"] == "alice"
    assert entry["actor_source"] == "principal"
    assert entry["actor_verified"] is True


def test_a_stated_actor_is_recorded_as_stated_in_a_single_user_deployment(
    example_settings: Settings, repo_root: Path, tmp_path: Path
) -> None:
    single = create_app(
        _settings(example_settings, repo_root, tmp_path, mode="proxy", single_user=True)
    )
    client = TestClient(single)
    _hold_proposal(single)

    moved = client.post("/proposals/p-1/transition", json={"to": "submitted", "by": "ana"})

    assert moved.status_code == 200, moved.text
    entry = moved.json()["proposal"]["history"][-1]
    assert (entry["by"], entry["actor_source"], entry["actor_verified"]) == (
        "ana",
        "stated",
        False,
    )
    unnamed = client.post("/proposals/p-1/transition", json={"to": "approved"})
    assert unnamed.status_code == 422


def test_a_proxy_principal_is_the_transition_actor(proxy_app: FastAPI) -> None:
    client = TestClient(proxy_app)
    _hold_proposal(proxy_app)

    moved = client.post(
        "/proposals/p-1/transition",
        json={"to": "submitted", "by": "mallory"},
        headers=_proxied("ana", roles=[REVIEWER]),
    )

    entry = moved.json()["proposal"]["history"][-1]
    assert (entry["by"], entry["actor_verified"]) == ("ana", True)


def test_the_command_line_records_the_operator_at_the_terminal(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    for name, value in (
        ("SKILLS_ROOTS", repo_root / "examples" / "ops" / "skills"),
        ("RUNTIME_CONFIG", repo_root / "configs" / "runtime.json"),
        ("MODEL_CONFIG", repo_root / "configs" / "model.json"),
        ("CONNECTORS_CONFIG", repo_root / "examples" / "ops" / "configs" / "connectors.json"),
        ("SCHEMAS_DIR", repo_root / "schemas"),
        ("STATE_DIR", state),
    ):
        monkeypatch.setenv(f"OSKA_{name}", str(value))
    monkeypatch.setenv("OSKA_PROVIDER", "none")
    store = ProposalStore(state / "proposals")
    store.save(SkillProposal(proposal_id="p-1", source_session_id="s", skill_markdown=MARKDOWN))

    moved = CliRunner().invoke(
        cli_app, ["propose", "transition", "p-1", "--to", "submitted", "--by", "ops.lead"]
    )

    assert moved.exit_code == 0, moved.output
    entry = store.get("p-1").history[-1]
    assert (entry.by, entry.actor_source, entry.actor_verified) == (
        "ops.lead",
        ActorSource.operator,
        True,
    )


# Required identity.


def test_token_mode_requires_an_identity_unless_the_configuration_says_otherwise() -> None:
    assert IdentityConfig().required is False
    assert IdentityConfig(mode="token").required is True
    assert IdentityConfig(mode="token", required=False).required is False
    assert IdentityConfig(mode="proxy", required=True).required is True


def test_required_identity_refuses_an_anonymous_call_in_proxy_mode(
    example_settings: Settings, repo_root: Path, tmp_path: Path
) -> None:
    client = TestClient(
        create_app(_settings(example_settings, repo_root, tmp_path, mode="proxy", required=True))
    )

    for path in ("/skills", "/capabilities", "/proposals", "/changes", "/domains"):
        refused = client.get(path)
        assert refused.status_code == 401, path
        assert refused.json()["error"] == "identity_required"
    assert client.get("/skills", headers={SUBJECT_HEADER: "ana"}).status_code == 200


def test_health_stays_open_and_metrics_do_not_when_identity_is_required(
    token_app: FastAPI,
) -> None:
    client = TestClient(token_app)

    assert client.get("/health").status_code == 200
    assert client.get("/metrics").status_code == 401
    assert client.get("/metrics", headers=_bearer("rita", "x", roles=[REVIEWER])).status_code == 200
    assert client.get("/skills").status_code == 401


# The subject a session was opened by.


def test_a_session_opened_by_one_subject_is_refused_to_another(proxy_app: FastAPI) -> None:
    client = TestClient(proxy_app)
    opened = client.post(
        "/sessions/owned/turns", json={"text": "hello"}, headers={SUBJECT_HEADER: "alice"}
    )
    assert opened.status_code == 200

    refusals = [
        client.get("/sessions/owned/receipts", headers={SUBJECT_HEADER: "bob"}),
        client.get("/sessions/owned/receipts"),
        client.post("/proposals/from-session/owned", headers={SUBJECT_HEADER: "bob"}),
        client.post("/proposals/from-session/owned"),
        client.post("/sessions/owned/turns", json={"text": "hi"}, headers={SUBJECT_HEADER: "bob"}),
        client.post("/sessions/owned/turns", json={"text": "hi"}),
    ]

    assert {response.status_code for response in refusals} == {403}
    assert len({response.text for response in refusals}) == 1
    assert "alice" not in refusals[0].text and "owned" not in refusals[0].text
    own = client.get("/sessions/owned/receipts", headers={SUBJECT_HEADER: "alice"})
    assert own.status_code == 200


def test_a_session_opened_without_an_identity_stays_open(proxy_app: FastAPI) -> None:
    client = TestClient(proxy_app)
    assert client.post("/sessions/open/turns", json={"text": "hello"}).status_code == 200
    assert client.get("/sessions/open/receipts").status_code == 200


# Only a reviewer reads proposals and change requests, or moves a proposal.


def _review_calls(client: TestClient, headers: dict[str, str]) -> list:
    """One call to every route a reviewer holds, all under the same headers."""
    return [
        client.get("/proposals", headers=headers),
        client.get("/proposals/p-1", headers=headers),
        client.post(
            "/proposals/p-1/transition", json={"to": "submitted", "by": "x"}, headers=headers
        ),
        client.get("/changes", headers=headers),
        client.get("/changes/any-request", headers=headers),
    ]


def test_a_token_without_the_reviewer_role_is_refused_every_review_route(
    token_app: FastAPI,
) -> None:
    client = TestClient(token_app)
    _hold_proposal(token_app)

    refused = _review_calls(client, _bearer("eve", "mine", roles=["operator"]))
    bare = _review_calls(client, _bearer("eve", "mine"))

    for response in [*refused, *bare]:
        assert response.status_code == 403
        assert response.json() == REFUSED_REVIEW
        assert "eve" not in response.text and "p-1" not in response.text
    assert _proposal(token_app).state.value == "draft"


def test_a_token_with_the_reviewer_role_reaches_every_review_route(token_app: FastAPI) -> None:
    client = TestClient(token_app)
    _hold_proposal(token_app)

    listed, one, moved, changes, change = _review_calls(
        client, _bearer("rita", "hers", roles=["operator", REVIEWER])
    )

    assert [entry["proposal_id"] for entry in listed.json()] == ["p-1"]
    assert one.status_code == 200
    assert moved.status_code == 200, moved.text
    assert moved.json()["proposal"]["history"][-1]["by"] == "rita"
    assert changes.status_code == 200
    assert change.status_code == 404
    assert _proposal(token_app).state.value == "submitted"


def test_a_proxy_principal_without_the_reviewer_role_is_refused_every_review_route(
    proxy_app: FastAPI,
) -> None:
    client = TestClient(proxy_app)
    _hold_proposal(proxy_app)

    for headers in (_proxied("eve"), _proxied("eve", roles=["operator"])):
        for response in _review_calls(client, headers):
            assert response.status_code == 403
            assert response.json() == REFUSED_REVIEW
    assert _proposal(proxy_app).state.value == "draft"


def test_a_proxy_principal_with_the_reviewer_role_reaches_every_review_route(
    proxy_app: FastAPI,
) -> None:
    client = TestClient(proxy_app)
    _hold_proposal(proxy_app)

    listed, one, moved, changes, change = _review_calls(
        client, _proxied("rita", roles=[REVIEWER])
    )

    assert [entry["proposal_id"] for entry in listed.json()] == ["p-1"]
    assert one.status_code == 200
    assert moved.status_code == 200, moved.text
    assert changes.status_code == 200
    assert change.status_code == 404


def test_a_single_user_deployment_reaches_every_review_route_without_an_identity(
    example_settings: Settings, repo_root: Path, tmp_path: Path
) -> None:
    single = create_app(
        _settings(example_settings, repo_root, tmp_path, mode="proxy", single_user=True)
    )
    client = TestClient(single)
    _hold_proposal(single)

    listed, one, moved, changes, change = _review_calls(client, {})

    assert [entry["proposal_id"] for entry in listed.json()] == ["p-1"]
    assert one.status_code == 200
    assert moved.status_code == 200, moved.text
    entry = moved.json()["proposal"]["history"][-1]
    assert (entry["by"], entry["actor_source"]) == ("x", "stated")
    assert changes.status_code == 200
    assert change.status_code == 404


def test_the_reviewer_role_is_the_one_the_configuration_names(
    example_settings: Settings, repo_root: Path, tmp_path: Path
) -> None:
    client = TestClient(
        create_app(
            _settings(example_settings, repo_root, tmp_path, mode="proxy", reviewer_role="curator")
        )
    )

    refused = client.get("/proposals", headers=_proxied("rita", roles=[REVIEWER]))
    allowed = client.get("/proposals", headers=_proxied("rita", roles=["curator"]))

    assert refused.status_code == 403
    assert refused.json() == {
        "error": "reviewer_required",
        "detail": "this call needs the curator role",
    }
    assert allowed.status_code == 200
    assert IdentityConfig().reviewer_role == REVIEWER


def test_a_session_s_own_token_and_a_reviewer_s_may_draft_from_it(token_app: FastAPI) -> None:
    client = TestClient(token_app)
    _alice_opens_victim(client)

    own = client.post("/proposals/from-session/victim", headers=_bearer("alice", "victim"))
    reviewed = client.post(
        "/proposals/from-session/victim", headers=_bearer("rita", "hers", roles=[REVIEWER])
    )
    other = client.post(
        "/proposals/from-session/victim", headers=_bearer("eve", "victim", roles=["operator"])
    )

    assert own.status_code == 200, own.text
    assert reviewed.status_code == 200, reviewed.text
    assert other.status_code == 403
    assert other.json()["error"] == "session_refused"


def test_a_session_s_own_principal_and_a_reviewer_may_draft_from_it_behind_a_proxy(
    proxy_app: FastAPI,
) -> None:
    client = TestClient(proxy_app)
    opened = client.post(
        "/sessions/owned/turns", json={"text": RESTART}, headers=_proxied("alice")
    )
    assert opened.status_code == 200

    own = client.post("/proposals/from-session/owned", headers=_proxied("alice"))
    reviewed = client.post(
        "/proposals/from-session/owned", headers=_proxied("rita", roles=[REVIEWER])
    )
    other = client.post(
        "/proposals/from-session/owned", headers=_proxied("bob", roles=["operator"])
    )

    assert own.status_code == 200, own.text
    assert reviewed.status_code == 200, reviewed.text
    assert other.status_code == 403
    assert other.json()["error"] == "session_refused"


# Tenants and domains on a platform.


def _platform(
    examples: Path, tmp_path: Path, settings: Settings, repo_root: Path, **identity
) -> TestClient:
    document = yaml.safe_load((examples / "platform.yaml").read_text(encoding="utf-8"))
    for domain in document["domains"]:
        domain["tenants"] = ["default", "acme"]
    root = tmp_path / "platform"
    root.mkdir()
    (root / "platform.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")
    for pack in ("ops", "access"):
        (root / pack).symlink_to(examples / pack)
    runtime = _runtime(repo_root, tmp_path / "platform-runtime.json", identity)
    held_settings = settings.model_copy(update={"runtime_config": runtime})
    held = Platform.from_file(root / "platform.yaml", held_settings)
    return TestClient(create_app(held_settings, platform=held))


def _tenants_used(settings: Settings) -> set[str]:
    state = Path(settings.state_dir)
    return {path.name for path in state.iterdir() if path.is_dir()} if state.is_dir() else set()


def test_the_tenant_is_not_taken_from_a_header_the_client_sets(
    examples: Path, tmp_path: Path, platform_settings: Settings, repo_root: Path
) -> None:
    client = _platform(examples, tmp_path, platform_settings, repo_root, mode="proxy")

    answered = client.post(
        "/domains/access/sessions/p/turns",
        json={"text": "hi"},
        headers={SUBJECT_HEADER: "ana", "X-Tenant": "acme"},
    )

    assert answered.status_code == 200
    assert "acme" not in _tenants_used(platform_settings)
    assert "default" in _tenants_used(platform_settings)


def test_the_tenant_header_is_honored_only_when_configured(
    examples: Path, tmp_path: Path, platform_settings: Settings, repo_root: Path
) -> None:
    client = _platform(
        examples,
        tmp_path,
        platform_settings,
        repo_root,
        mode="proxy",
        tenant_header="X-Proxy-Tenant",
    )

    ignored = client.post(
        "/domains/access/sessions/p/turns",
        json={"text": "hi"},
        headers={SUBJECT_HEADER: "ana", "X-Tenant": "acme"},
    )
    assert ignored.status_code == 200
    assert "acme" not in _tenants_used(platform_settings)

    honored = client.post(
        "/domains/access/sessions/q/turns",
        json={"text": "hi"},
        headers={SUBJECT_HEADER: "ana", "X-Proxy-Tenant": "acme"},
    )
    assert honored.status_code == 200
    assert "acme" in _tenants_used(platform_settings)

    attribute = client.post(
        "/domains/access/sessions/r/turns",
        json={"text": "hi"},
        headers={
            SUBJECT_HEADER: "ana",
            ATTRIBUTES_HEADER: json.dumps({"tenant": "default"}),
            "X-Proxy-Tenant": "acme",
        },
    )
    assert attribute.status_code == 200


def test_a_domain_crossing_is_indistinguishable_from_any_other_refusal(
    examples: Path, tmp_path: Path, platform_settings: Settings, repo_root: Path
) -> None:
    client = _platform(examples, tmp_path, platform_settings, repo_root, mode="proxy")
    opened = client.post(
        "/domains/access/sessions/web/turns",
        json={"text": "hello"},
        headers={SUBJECT_HEADER: "alice"},
    )
    assert opened.status_code == 200

    same_domain = client.post("/domains/access/sessions/web/turns", json={"text": "hi"})
    other_domain = client.post("/domains/operations/sessions/web/turns", json={"text": "hi"})

    assert same_domain.status_code == other_domain.status_code == 403
    assert same_domain.json() == other_domain.json()
    assert "access" not in other_domain.text


# Who a commit is authored as.


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


def _repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-global-config"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    root = tmp_path / "catalog-repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.name", "Operator")
    _git(root, "config", "user.email", "operator@example.org")
    (root / "README.md").write_text("catalog\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "initial")
    return root


def _request(store: ChangeRequestStore, **actor: object) -> str:
    request = new_request(
        "proposal-1", "runbook.sample", 1, "Ada <ada@example.org>", QualityReport(score=1.0),
        **actor,
    )
    store.save(request, skill_markdown=MARKDOWN, policy=POLICY)
    return request.request_id


def test_the_git_publisher_refuses_an_unverified_actor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _repository(tmp_path, monkeypatch)
    store = ChangeRequestStore(tmp_path / "changes")
    request_id = _request(store)

    with pytest.raises(ChangeRequestError, match="not verified"):
        GitPublisher(repository).publish(store.get(request_id), store)
    assert store.get(request_id).branch == ""

    allowed = GitPublisher(repository, allow_unverified_actor=True).publish(
        store.get(request_id), store
    )
    assert allowed.branch
    assert "stated, not verified" in _git(repository, "log", "-1", "--format=%B", allowed.branch)


def test_the_git_publisher_authors_a_verified_actor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _repository(tmp_path, monkeypatch)
    store = ChangeRequestStore(tmp_path / "changes")
    request_id = _request(store, actor_source=ActorSource.principal)

    published = GitPublisher(repository).publish(store.get(request_id), store)

    assert _git(repository, "log", "-1", "--format=%an", published.branch) == "Ada"


def _hold_proposal(app: FastAPI) -> None:
    app.state.services.proposals.save(
        SkillProposal(proposal_id="p-1", source_session_id="s", skill_markdown=MARKDOWN)
    )


def _proposal(app: FastAPI) -> SkillProposal:
    return app.state.services.proposals.get("p-1")


# A session opened without an identity stays without one.


def _scripted_app(settings: Settings, repo_root: Path, tmp_path: Path) -> FastAPI:
    """The shipped single-user configuration, over the recorded restart."""
    scenarios = {
        scenario.name: scenario
        for scenario in load_scenarios(repo_root / "examples" / "ops" / "scenarios")
    }
    engine = build_engine(
        settings,
        provider=ScriptedModelProvider(scenarios["02_restart_staging"].provider_script[:3]),
        audit_sink=MemorySink(),
    )
    return create_app(settings, engine=engine, proposal_store=ProposalStore(tmp_path / "p"))


def test_an_identity_cannot_answer_the_approval_of_a_session_opened_without_one(
    example_settings: Settings, repo_root: Path, tmp_path: Path
) -> None:
    app = _scripted_app(example_settings, repo_root, tmp_path)
    engine = app.state.services.engine
    client = TestClient(app)
    opened = client.post("/sessions/fixed/turns", json={"text": RESTART})
    assert opened.status_code == 200
    assert opened.json()["pending"] is not None

    taken = client.post("/sessions/fixed/turns", json={"text": "yes"}, headers=_proxied("alice"))

    assert taken.status_code == 403
    assert taken.json() == {
        "error": "session_refused",
        "detail": "this call may not act in this session",
    }
    assert engine.receipts("fixed") == []
    assert engine.state("fixed").principal == {}
    assert engine.state("fixed").pending_approval is not None
    answered = client.post("/sessions/fixed/turns", json={"text": "yes"})
    assert answered.status_code == 200
    assert len(engine.receipts("fixed")) == 1
    assert engine.state("fixed").principal == {}


@pytest.mark.parametrize("begun", ["history", "turn", "goal", "approval"])
def test_an_identity_does_not_bind_to_a_session_that_has_begun(begun: str) -> None:
    state = SessionState(session_id="s")
    if begun == "history":
        state.history.append(Message(role="user", content="hello"))
    elif begun == "turn":
        state.turn_count = 1
    elif begun == "goal":
        state.pending_goal = PendingGoal.model_construct()
    else:
        state.pending_approval = PendingApproval.model_construct()

    with pytest.raises(PrincipalMismatch):
        bind(state, Principal(subject="alice", authenticated=True))
    assert state.principal == {}
    bind(state, None)


def test_an_identity_binds_to_a_session_on_its_first_turn() -> None:
    state = SessionState(session_id="s")

    bind(state, Principal(subject="alice", authenticated=True))

    assert Principal.from_dict(state.principal).subject == "alice"


# No identity is never worth more than an identity.


def test_single_user_is_off_by_default_and_refused_in_token_mode() -> None:
    assert IdentityConfig().single_user is False
    assert IdentityConfig(mode="proxy", single_user=True).single_user is True
    with pytest.raises(ValueError, match="single_user"):
        IdentityConfig(mode="token", required=False, single_user=True)


def test_without_single_user_an_anonymous_call_does_not_reach_the_review_routes(
    proxy_app: FastAPI,
) -> None:
    client = TestClient(proxy_app)
    _hold_proposal(proxy_app)

    for response in _review_calls(client, {}):
        assert response.status_code == 401
        assert response.json()["error"] == "identity_required"
    assert _proposal(proxy_app).state.value == "draft"
    assert client.post("/sessions/open/turns", json={"text": "hello"}).status_code == 200
    assert client.get("/sessions/open/receipts").status_code == 200
    drafted = client.post("/proposals/from-session/open")
    assert drafted.status_code == 401
    assert proxy_app.state.services.proposals.list() == [_proposal(proxy_app)]
    reviewed = client.post("/proposals/from-session/open", headers=_proxied("rita", [REVIEWER]))
    assert reviewed.status_code == 200, reviewed.text


def test_without_single_user_an_anonymous_token_call_does_not_reach_the_review_routes(
    example_settings: Settings, repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(KEY_ENV, KEY)
    app = create_app(
        _settings(
            example_settings, repo_root, tmp_path, mode="token", key_env=KEY_ENV, required=False
        )
    )
    client = TestClient(app)
    _hold_proposal(app)

    anonymous = _review_calls(client, {})
    identified = _review_calls(client, _bearer("bob", "b"))

    assert {response.status_code for response in anonymous} == {401}
    assert {response.status_code for response in identified} == {403}
    assert _proposal(app).state.value == "draft"


# The tenant header, once named, is the only source of the tenant.


def test_a_named_tenant_header_is_not_overridden_by_the_attributes(
    examples: Path, tmp_path: Path, platform_settings: Settings, repo_root: Path
) -> None:
    client = _platform(
        examples, tmp_path, platform_settings, repo_root, mode="proxy",
        tenant_header="X-Proxy-Tenant",
    )

    answered = client.post(
        "/domains/access/sessions/p/turns",
        json={"text": "hi"},
        headers={
            SUBJECT_HEADER: "mallory",
            ATTRIBUTES_HEADER: json.dumps({"tenant": "acme"}),
            "X-Proxy-Tenant": "default",
        },
    )
    unnamed = client.post(
        "/domains/access/sessions/q/turns",
        json={"text": "hi"},
        headers={SUBJECT_HEADER: "mallory", ATTRIBUTES_HEADER: json.dumps({"tenant": "acme"})},
    )

    assert answered.status_code == unnamed.status_code == 200
    assert "acme" not in _tenants_used(platform_settings)


# What the runtime says about its tenants, it says to a reviewer.


def test_the_domains_name_their_tenants_only_to_a_reviewer(
    examples: Path, tmp_path: Path, platform_settings: Settings, repo_root: Path
) -> None:
    client = _platform(examples, tmp_path, platform_settings, repo_root, mode="proxy")

    anyone = client.get("/domains", headers=_proxied("eve")).json()
    anonymous = client.get("/domains").json()
    reviewer = client.get("/domains", headers=_proxied("rita", [REVIEWER])).json()

    assert [entry["name"] for entry in anyone] == [entry["name"] for entry in reviewer]
    assert all(entry["tenants"] == [] for entry in [*anyone, *anonymous])
    assert all(entry["tenants"] == ["default", "acme"] for entry in reviewer)


def test_metrics_are_a_reviewer_s_read_where_an_identity_is_required(
    example_settings: Settings, repo_root: Path, tmp_path: Path
) -> None:
    client = TestClient(
        create_app(_settings(example_settings, repo_root, tmp_path, mode="proxy", required=True))
    )

    assert client.get("/metrics").status_code == 401
    assert client.get("/metrics", headers=_proxied("eve")).json() == REFUSED_REVIEW
    assert client.get("/metrics", headers=_proxied("rita", [REVIEWER])).status_code == 200
    assert client.get("/health").status_code == 200


def test_public_metrics_stay_open_where_an_identity_is_required(
    example_settings: Settings, repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(KEY_ENV, KEY)
    settings = _settings(example_settings, repo_root, tmp_path, mode="token", key_env=KEY_ENV)
    document = json.loads(Path(settings.runtime_config).read_text(encoding="utf-8"))
    document["metrics"]["public"] = True
    Path(settings.runtime_config).write_text(json.dumps(document), encoding="utf-8")
    client = TestClient(create_app(settings))

    assert client.get("/metrics").status_code == 200
    assert client.get("/skills").status_code == 401


def test_an_identity_cannot_draft_from_a_session_opened_without_one(
    example_settings: Settings, repo_root: Path, tmp_path: Path
) -> None:
    app = _scripted_app(example_settings, repo_root, tmp_path)
    client = TestClient(app)
    assert client.post("/sessions/open/turns", json={"text": RESTART}).status_code == 200

    taken = client.post("/proposals/from-session/open", headers=_proxied("alice"))

    assert taken.status_code == 403
    assert taken.json()["error"] == "session_refused"
    assert app.state.services.proposals.list() == []


# Proving the proxy sent the identity.

PROXY_SECRET_ENV = "OSKA_TEST_PROXY_SECRET"


@pytest.fixture
def guarded_proxy_app(
    example_settings: Settings, repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> FastAPI:
    """Proxy identity that only takes headers arriving with the proxy's secret."""
    monkeypatch.setenv(PROXY_SECRET_ENV, "s3cret-shared-with-the-proxy")
    return create_app(
        _settings(
            example_settings,
            repo_root,
            tmp_path,
            mode="proxy",
            required=True,
            proxy_secret_env=PROXY_SECRET_ENV,
        )
    )


def test_identity_headers_without_the_proxy_secret_are_refused(
    guarded_proxy_app: FastAPI,
) -> None:
    """A caller that reaches the port around the proxy cannot claim to be a reviewer."""
    client = TestClient(guarded_proxy_app)

    forged = client.get("/proposals", headers=_proxied("mallory", roles=[REVIEWER]))
    assert forged.status_code == 401
    assert forged.json()["error"] == "proxy_unverified"
    wrong = {**_proxied("mallory", roles=[REVIEWER]), "X-Proxy-Secret": "guess"}
    assert client.get("/proposals", headers=wrong).status_code == 401


def test_identity_headers_with_the_proxy_secret_are_taken(guarded_proxy_app: FastAPI) -> None:
    client = TestClient(guarded_proxy_app)
    secret = {"X-Proxy-Secret": "s3cret-shared-with-the-proxy"}
    headers = {**_proxied("rita", roles=[REVIEWER]), **secret}

    assert client.get("/proposals", headers=headers).status_code == 200
    assert client.get("/skills").status_code == 401
    assert client.get("/health").status_code == 200


def test_a_named_proxy_secret_that_is_not_set_is_a_server_error(
    example_settings: Settings, repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(PROXY_SECRET_ENV, raising=False)
    client = TestClient(
        create_app(
            _settings(
                example_settings,
                repo_root,
                tmp_path,
                mode="proxy",
                proxy_secret_env=PROXY_SECRET_ENV,
            )
        )
    )

    refused = client.get("/skills", headers=_proxied("rita"))
    assert refused.status_code == 500
    assert refused.json()["error"] == "identity_key_missing"
    assert PROXY_SECRET_ENV in refused.json()["detail"]


def test_a_proxy_secret_is_refused_outside_proxy_mode() -> None:
    with pytest.raises(ValueError, match="proxy mode only"):
        IdentityConfig(mode="token", proxy_secret_env=PROXY_SECRET_ENV)


def test_serve_warns_when_a_public_port_trusts_proxy_headers_without_a_secret() -> None:
    from oska.cli.commands_serve import proxy_trust_warning, trusts_unverified_proxy

    assert trusts_unverified_proxy(IdentityConfig(required=True), "0.0.0.0")
    assert not trusts_unverified_proxy(IdentityConfig(required=True), "127.0.0.1")
    assert not trusts_unverified_proxy(
        IdentityConfig(required=True, proxy_secret_env=PROXY_SECRET_ENV), "0.0.0.0"
    )
    assert "proxy_secret_env" in proxy_trust_warning("0.0.0.0", 8000)
