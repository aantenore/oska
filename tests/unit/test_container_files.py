"""The container files, read as data.

The image is a demo, but it must not undo the properties the rest of the
repository holds: it runs as a user that is not root, its base images are named
by a tag that does not move to whatever is newest, it can say whether it is
healthy, and neither the image nor the build context carries the real `.env`.
The compose file publishes the API on the loopback address only.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO_ROOT / "Dockerfile"
DOCKERIGNORE = REPO_ROOT / ".dockerignore"
COMPOSE = REPO_ROOT / "compose.yaml"
COMPOSE_RUNTIME = REPO_ROOT / "deploy" / "compose" / "runtime.json"
LOOPBACK = "127.0.0.1"


def _instructions() -> list[tuple[str, str]]:
    """The Dockerfile as (INSTRUCTION, arguments) pairs, continuation lines joined."""
    joined: list[str] = []
    pending = ""
    for raw in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not pending and (not line or line.startswith("#")):
            continue
        if line.endswith("\\"):
            pending += line[:-1] + " "
            continue
        joined.append(pending + line)
        pending = ""
    pairs = []
    for statement in joined:
        keyword, _, arguments = statement.partition(" ")
        pairs.append((keyword.upper(), arguments.strip()))
    return pairs


def _arguments(keyword: str) -> list[str]:
    return [arguments for name, arguments in _instructions() if name == keyword]


def test_base_images_are_pinned_by_a_tag() -> None:
    stages = _arguments("FROM")
    assert len(stages) >= 2, "the build and the runtime are separate stages"
    for stage in stages:
        image = stage.split()[0]
        assert ":" in image, f"{image} names no tag"
        tag = image.rsplit(":", 1)[1]
        assert tag and tag != "latest", f"{image} does not pin a tag"


def test_the_runtime_stage_runs_as_a_user_that_is_not_root() -> None:
    users = _arguments("USER")
    assert users, "the image never leaves root"
    final = users[-1].split(":")[0]
    assert final not in {"root", "0"}
    last_from = max(i for i, (name, _) in enumerate(_instructions()) if name == "FROM")
    after = [name for name, _ in _instructions()[last_from:]]
    assert "USER" in after, "the user is set in the stage that ships, not an earlier one"


def test_the_image_declares_a_health_check_and_a_state_volume() -> None:
    health = _arguments("HEALTHCHECK")
    assert health and "/health" in health[-1]
    assert "curl" not in health[-1], "the check uses the standard library, not curl"
    assert any("/srv/oska/state" in argument for argument in _arguments("VOLUME"))
    assert any("OSKA_STATE_DIR=/srv/oska/state" in a for a in _arguments("ENV"))
    assert _arguments("WORKDIR")[-1] == "/srv/oska"


def test_the_image_does_not_copy_the_real_env_file() -> None:
    for keyword in ("COPY", "ADD"):
        for arguments in _arguments(keyword):
            sources = [t for t in arguments.split() if not t.startswith("--")][:-1]
            for source in sources:
                name = source.rstrip("/").rsplit("/", 1)[-1]
                assert name != ".env", f"{keyword} {arguments} copies the real .env"
                assert source not in {".", "./"} or _ignored(".env"), "a whole-context copy"


def _ignored(name: str) -> bool:
    return name in _ignore_patterns()


def _ignore_patterns() -> list[str]:
    lines = DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.startswith("#")]


def test_the_build_context_leaves_out_secrets_and_history() -> None:
    patterns = _ignore_patterns()
    assert ".env" in patterns
    assert ".git" in patterns
    assert "state/" in patterns


def test_compose_publishes_on_the_loopback_address_only() -> None:
    document = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    services = document["services"]
    assert {"oska", "postgres"} <= set(services)
    published = 0
    for name, service in services.items():
        for port in service.get("ports", []):
            published += 1
            assert str(port).startswith(f"{LOOPBACK}:"), f"{name} publishes {port}"
    assert published >= 1, "the API is published at all"
    assert services["oska"]["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert "healthcheck" in services["postgres"]
    assert re.fullmatch(r"postgres:\d+", services["postgres"]["image"])


def test_compose_keeps_the_database_address_out_of_the_runtime_file() -> None:
    document = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    environment = document["services"]["oska"]["environment"]
    runtime = json.loads(COMPOSE_RUNTIME.read_text(encoding="utf-8"))
    assert runtime["storage"]["backend"] == "postgres"
    assert "://" not in COMPOSE_RUNTIME.read_text(encoding="utf-8")
    assert environment["OSKA_DATABASE_URL"].startswith("postgresql://")
    assert environment["OSKA_RUNTIME_CONFIG"] == "/srv/oska/runtime.json"
