"""The page at `GET /`: served without an identity, loading nothing from elsewhere.

The page holds a conversation through the API the way any client does: every
call it makes is one an identity check sees. It must load nothing from another
origin, its policy must admit exactly the script and style it carries, and it
must never put server text into the document as markup. Served from a
replayed recording, the endpoints it reads must answer what it expects: the
recorded requests, the approval words of the conversation's language, the
held write with its exact payload and digest, and the verified receipt.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from importlib import resources
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from oska.api.app import PAGE_PATH, create_app
from oska.config import Settings
from oska.governance.identity import mint_token
from oska.planner.recording import KeyedReplayProvider

REQUEST = "Il servizio search in staging è bloccato, riavvialo per favore"
CASSETTE = Path("examples") / "ops" / "cassettes" / "restart-in-italian.json"
KEY_ENV = "WEB_PAGE_TEST_KEY"
KEY = "w" * 32
CSP = re.compile(r'<meta http-equiv="Content-Security-Policy" content="([^"]+)">')
MARKUP_SINKS = ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "srcdoc")


def _page() -> str:
    return resources.files("oska.api").joinpath(PAGE_PATH).read_text(encoding="utf-8")


def _runtime(repo_root: Path, target: Path, **blocks: dict) -> Path:
    """A copy of the shipped runtime configuration with these blocks in place of its own."""
    document = json.loads((repo_root / "configs" / "runtime.json").read_text(encoding="utf-8"))
    document.update(blocks)
    document["guardrails"]["catalog"] = str(repo_root / "configs" / "guardrails.json")
    target.write_text(json.dumps(document), encoding="utf-8")
    return target


def _client(settings: Settings, **kwargs: object) -> TestClient:
    return TestClient(create_app(settings, **kwargs))  # type: ignore[arg-type]


def _digest(body: str) -> str:
    return "sha256-" + base64.b64encode(hashlib.sha256(body.encode("utf-8")).digest()).decode()


def test_the_page_is_served_as_html_with_a_policy_and_nothing_from_elsewhere(
    example_settings: Settings,
) -> None:
    response = _client(example_settings).get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    page = response.text
    assert page == _page()
    policy = CSP.search(page)
    assert policy is not None
    assert "default-src 'none'" in policy.group(1)
    assert "connect-src 'self'" in policy.group(1)
    assert re.search(r"https?:|(?<![:\w])//\w", page) is None


def test_the_policy_admits_exactly_the_script_and_style_the_page_carries() -> None:
    page = _page()
    policy = CSP.search(page)
    assert policy is not None
    scripts = re.findall(r"<script>(.*?)</script>", page, flags=re.S)
    styles = re.findall(r"<style>(.*?)</style>", page, flags=re.S)
    assert len(scripts) == 1 and len(styles) == 1
    assert f"script-src '{_digest(scripts[0])}'" in policy.group(1), (
        "the inline script changed: put its sha256 in the page's Content-Security-Policy"
    )
    assert f"style-src '{_digest(styles[0])}'" in policy.group(1), (
        "the inline style changed: put its sha256 in the page's Content-Security-Policy"
    )
    assert " style=" not in page


def test_the_script_never_puts_text_into_the_document_as_markup() -> None:
    page = _page()
    for sink in MARKUP_SINKS:
        assert sink not in page, sink
    assert "textContent" in page


def test_api_ui_false_serves_no_page(
    example_settings: Settings, repo_root: Path, tmp_path: Path
) -> None:
    runtime = _runtime(repo_root, tmp_path / "runtime.json", api={"ui": False})
    client = _client(example_settings.model_copy(update={"runtime_config": runtime}))

    assert client.get("/").status_code == 404
    assert client.get("/health").status_code == 200


def test_the_page_needs_no_identity_and_the_calls_it_makes_still_do(
    example_settings: Settings, repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(KEY_ENV, KEY)
    runtime = _runtime(
        repo_root, tmp_path / "runtime.json", identity={"mode": "token", "key_env": KEY_ENV}
    )
    client = _client(example_settings.model_copy(update={"runtime_config": runtime}))
    token = mint_token({"sub": "ada", "sid": "web-1", "exp": 4_000_000_000}, KEY)

    assert client.get("/").status_code == 200
    assert client.get("/health").status_code == 200
    refused = client.get("/capabilities")
    assert refused.status_code == 401
    assert refused.json()["error"] == "identity_required"
    assert client.post("/sessions/web-1/turns", json={"text": "hello"}).status_code == 401
    answered = client.get("/capabilities", headers={"Authorization": f"Bearer {token}"})
    assert answered.status_code == 200


def test_without_a_replay_there_are_no_recorded_prompts(example_settings: Settings) -> None:
    assert _client(example_settings).get("/demo/prompts").json() == []


def test_the_endpoints_the_page_reads_answer_what_it_expects_from_a_replay(
    example_settings: Settings, repo_root: Path
) -> None:
    client = _client(example_settings, provider=KeyedReplayProvider(repo_root / CASSETTE))

    assert client.get("/health").json()["provider"] == "replay"
    assert client.get("/demo/prompts").json() == [REQUEST]
    capabilities = client.get("/capabilities").json()
    assert capabilities["processes"] and all(p["action"] for p in capabilities["processes"])
    assert capabilities["default_language"] == "en"
    assert capabilities["approval_words"]["en"] == {"approve": "yes", "reject": "no"}
    assert capabilities["approval_words"]["it"] == {"approve": "sì", "reject": "no"}

    asked = client.post("/sessions/web-1/turns", json={"text": REQUEST}).json()
    approval = asked["pending"]["approval"]
    assert asked["pending"]["kind"] == "approval"
    assert approval["operation"] == "service.restart"
    assert approval["integration_id"] == "integration.container_runtime"
    assert approval["exact_payload"] == {"service": "search"}
    assert re.fullmatch(r"[0-9a-f]{64}", approval["payload_hash"])
    language = approval["plan"]["language"]
    word = capabilities["approval_words"][language]["approve"]

    approved = client.post("/sessions/web-1/turns", json={"text": word}).json()
    receipts = [o for o in approved["outcomes"] if o["kind"] == "receipt"]
    assert approved["pending"] is None
    assert [r["data"]["verification"] for r in receipts] == ["verified"]
    listed = client.get("/sessions/web-1/receipts").json()
    assert [(r["receipt_id"], r["verification"]) for r in listed] == [
        (receipts[0]["receipt_id"], "verified")
    ]


def test_the_page_cannot_be_framed_by_another_site(example_settings: Settings) -> None:
    """A page with an Approve button refuses to be drawn inside someone else's page."""
    headers = _client(example_settings).get("/").headers

    assert headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    assert headers["x-content-type-options"] == "nosniff"
