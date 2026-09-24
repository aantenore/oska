"""A signed revision: who vouched for it, and what a runtime refuses to serve."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from oska.catalog.registry import (
    CatalogUnavailable,
    ServedCatalog,
    SkillRegistry,
    serve_catalog,
)
from oska.catalog.revision import MANIFEST_FILENAME, RevisionStore, build_revision
from oska.catalog.signing import (
    HMAC_SHA256,
    SIGNATURE_FILENAME,
    HmacSha256Signer,
    RevisionTrust,
    RevisionTrustError,
    register_signer,
    signature_document,
    signer_for,
)
from oska.cli.commands_revision import app
from oska.config import CatalogConfig, Settings, SigningConfig
from oska.governance.ledger import WriteLedger
from oska.governance.policy_engine import PolicyEngine, PolicyError

TENANT = "default"
KEY_ENV = "OSKA_TEST_REVISION_KEY"
KEY = "revision-key"
RESTART = "runbook.service_restart"
runner = CliRunner()


def _signing(*, required: bool = False, signer: str = HMAC_SHA256) -> SigningConfig:
    return SigningConfig(signer=signer, key_env=KEY_ENV, required=required)


def _store(root: Path, *, required: bool = False, key: str = KEY) -> RevisionStore:
    return RevisionStore(root, trust=RevisionTrust(_signing(required=required), {KEY_ENV: key}))


def _build(settings: Settings, root: Path, *, signed: bool = True, key: str = KEY) -> str:
    signer = signer_for(_signing(), {KEY_ENV: key}) if signed else None
    manifest = build_revision(
        settings.skills_roots, root, tenant=TENANT, settings=settings, signer=signer
    )
    return manifest.revision


@pytest.fixture
def store_root(tmp_path: Path) -> Path:
    return tmp_path / "state" / "revisions"


def test_a_signature_verifies_the_bytes_it_was_made_over_and_no_others() -> None:
    signer = HmacSha256Signer(KEY.encode())
    signature = signer.sign(b'{"revision": "abc"}')
    assert signer.verify(b'{"revision": "abc"}', signature)
    assert not signer.verify(b'{"revision": "abd"}', signature)
    assert not HmacSha256Signer(b"another").verify(b'{"revision": "abc"}', signature)


def test_a_signed_revision_is_served_after_it_verifies(
    example_settings: Settings, store_root: Path
) -> None:
    revision = _build(example_settings, store_root)
    directory = store_root / TENANT / revision
    assert (directory / SIGNATURE_FILENAME).is_file()
    manifest = json.loads((directory / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    assert manifest["files"]
    assert _store(store_root, required=True).verify(TENANT, revision) is True
    registry = SkillRegistry.from_revision(_store(store_root, required=True), TENANT, revision)
    assert registry.current()


def test_one_byte_changed_in_the_manifest_is_refused(
    example_settings: Settings, store_root: Path
) -> None:
    revision = _build(example_settings, store_root)
    path = store_root / TENANT / revision / MANIFEST_FILENAME
    raw = bytearray(path.read_bytes())
    raw[-2] = ord(" ") if raw[-2] != ord(" ") else ord("\t")
    path.write_bytes(bytes(raw))
    with pytest.raises(RevisionTrustError, match="does not match its manifest"):
        _store(store_root).verify(TENANT, revision)


def test_a_skill_file_changed_under_a_signed_manifest_is_refused(
    example_settings: Settings, store_root: Path
) -> None:
    revision = _build(example_settings, store_root)
    skill = next((store_root / TENANT / revision).rglob("skill.md"))
    skill.write_text(skill.read_text(encoding="utf-8") + "\nOne more line.\n", encoding="utf-8")
    with pytest.raises(RevisionTrustError, match="differ from its signed manifest"):
        SkillRegistry.from_revision(_store(store_root), TENANT, revision)


def test_a_file_added_under_a_signed_manifest_is_refused(
    example_settings: Settings, store_root: Path
) -> None:
    revision = _build(example_settings, store_root)
    (store_root / TENANT / revision / "extra.md").write_text("unlisted", encoding="utf-8")
    with pytest.raises(RevisionTrustError, match="extra.md"):
        _store(store_root).verify(TENANT, revision)


def test_a_revision_checked_with_the_wrong_key_is_refused(
    example_settings: Settings, store_root: Path
) -> None:
    revision = _build(example_settings, store_root)
    with pytest.raises(RevisionTrustError):
        _store(store_root, key="not-the-key").verify(TENANT, revision)


def test_an_unsigned_revision_is_refused_only_when_signing_is_required(
    example_settings: Settings, store_root: Path
) -> None:
    revision = _build(example_settings, store_root, signed=False)
    assert not (store_root / TENANT / revision / SIGNATURE_FILENAME).exists()
    assert _store(store_root, required=False).verify(TENANT, revision) is False
    with pytest.raises(RevisionTrustError, match="not signed"):
        _store(store_root, required=True).verify(TENANT, revision)


def test_a_signed_revision_is_verified_even_when_signing_is_not_required(
    example_settings: Settings, store_root: Path
) -> None:
    revision = _build(example_settings, store_root)
    trust = RevisionTrust(SigningConfig(key_env=KEY_ENV), {KEY_ENV: "not-the-key"})
    with pytest.raises(RevisionTrustError):
        RevisionStore(store_root, trust=trust).verify(TENANT, revision)


def test_a_missing_key_is_a_refusal_and_not_a_quiet_default() -> None:
    with pytest.raises(RevisionTrustError, match=KEY_ENV):
        signer_for(_signing(), {})


class _Sha1Signer:
    """A second signer, distinct from the built-in one, plugged in by `register_signer`."""

    name = "hmac-sha1"

    def __init__(self, key: bytes) -> None:
        self._key = key

    def sign(self, manifest_bytes: bytes) -> str:
        digest = hmac.new(self._key, manifest_bytes, hashlib.sha1).digest()
        return digest.hex()

    def verify(self, manifest_bytes: bytes, signature: str) -> bool:
        return hmac.compare_digest(self.sign(manifest_bytes), signature)


def test_a_second_signer_registered_by_name_is_used_when_configured(tmp_path: Path) -> None:
    """OSKA-SKL-33: a signer registered under another name signs and is checked on load,
    through the same `signer_for`/`RevisionTrust.check` path the built-in signer uses.
    """
    register_signer(_Sha1Signer.name, lambda config, env: _Sha1Signer(env[KEY_ENV].encode()))
    config = _signing(signer=_Sha1Signer.name)

    signer = signer_for(config, {KEY_ENV: KEY})
    assert isinstance(signer, _Sha1Signer)

    manifest_bytes = b'{"revision": "from-a-second-signer"}'
    directory = tmp_path / "revision"
    directory.mkdir()
    (directory / MANIFEST_FILENAME).write_bytes(manifest_bytes)
    (directory / SIGNATURE_FILENAME).write_bytes(signature_document(signer, manifest_bytes))

    assert RevisionTrust(config, {KEY_ENV: KEY}).check(directory, manifest_bytes) is True
    tampered = manifest_bytes.replace(b"from-a-second-signer", b"tampered")
    with pytest.raises(RevisionTrustError, match="does not match its manifest"):
        RevisionTrust(config, {KEY_ENV: KEY}).check(directory, tampered)


def test_building_again_with_a_signer_signs_a_revision_already_held(
    example_settings: Settings, store_root: Path
) -> None:
    unsigned = _build(example_settings, store_root, signed=False)
    signed = _build(example_settings, store_root)
    assert signed == unsigned
    assert _store(store_root, required=True).verify(TENANT, signed) is True


def _catalog(mode: str, *, required: bool = False) -> CatalogConfig:
    return CatalogConfig(mode=mode, signing=_signing(required=required))


def test_revision_mode_refuses_to_start_without_a_revision(example_settings: Settings) -> None:
    with pytest.raises(CatalogUnavailable, match="revision build"):
        serve_catalog(_catalog("revision"), example_settings, env={KEY_ENV: KEY})


def test_revision_mode_serves_a_verified_revision(
    example_settings: Settings, store_root: Path
) -> None:
    revision = _build(example_settings, store_root)
    served = serve_catalog(
        _catalog("revision", required=True), example_settings, env={KEY_ENV: KEY}
    )
    assert isinstance(served, ServedCatalog)
    assert served.revision == revision


def test_revision_mode_refuses_to_start_on_a_revision_that_does_not_verify(
    example_settings: Settings, store_root: Path
) -> None:
    _build(example_settings, store_root, signed=False)
    with pytest.raises(RevisionTrustError):
        serve_catalog(_catalog("revision", required=True), example_settings, env={KEY_ENV: KEY})


def test_directory_mode_serves_the_roots_when_no_revision_is_held(
    example_settings: Settings,
) -> None:
    served = serve_catalog(_catalog("directory"), example_settings, env={})
    assert isinstance(served, SkillRegistry)
    assert served.roots == example_settings.skills_roots


def test_the_cli_signs_verifies_and_refuses(
    repo_root: Path, catalog_copy: Path, tmp_path: Path
) -> None:
    runtime = json.loads((repo_root / "configs" / "runtime.json").read_text(encoding="utf-8"))
    runtime["catalog"]["signing"] = {"signer": HMAC_SHA256, "key_env": KEY_ENV, "required": True}
    config = tmp_path / "runtime.json"
    config.write_text(json.dumps(runtime), encoding="utf-8")
    env = {
        "OSKA_STATE_DIR": str(tmp_path / "state"),
        "OSKA_RUNTIME_CONFIG": str(config),
        "OSKA_SCHEMAS_DIR": str(repo_root / "schemas"),
        KEY_ENV: KEY,
    }
    where = ["--skills", str(catalog_copy)]
    built = runner.invoke(app, ["build", *where], env=env)
    assert built.exit_code == 0, built.output
    assert f"signed by {HMAC_SHA256}" in built.output
    revision = built.output.split("\t", 1)[0]
    verified = runner.invoke(app, ["verify", revision, *where], env=env)
    assert verified.exit_code == 0, verified.output
    assert "signed and verified" in verified.output
    wrong = runner.invoke(app, ["use", revision, *where], env={**env, KEY_ENV: "not-the-key"})
    assert wrong.exit_code == 1
    assert "would be refused" in wrong.output
    unkeyed = runner.invoke(app, ["build", *where], env={**env, KEY_ENV: ""})
    assert unkeyed.exit_code == 1
    assert KEY_ENV in unkeyed.output


def _changed_after_verification(monkeypatch: pytest.MonkeyPatch, store_root: Path) -> Path:
    """Let the store verify a revision, then change one of its skill files straight after.

    That is the window a file read twice leaves open: checked once, parsed later.
    """
    verify = RevisionStore.verify
    target: list[Path] = []

    def verify_then_change(self: RevisionStore, tenant: str, revision: str) -> bool:
        signed = verify(self, tenant, revision)
        path = self.path(tenant, revision) / "runbook" / "service_restart" / "v1" / "skill.md"
        text = path.read_text(encoding="utf-8")
        path.write_text(
            text.replace("description:", "description: Changed after the check.", 1),
            encoding="utf-8",
        )
        target.append(path)
        return signed

    monkeypatch.setattr(RevisionStore, "verify", verify_then_change)
    return store_root


def test_a_file_changed_between_verification_and_parse_refuses_the_revision(
    example_settings: Settings, store_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    revision = _build(example_settings, store_root)
    _changed_after_verification(monkeypatch, store_root)
    with pytest.raises(RevisionTrustError, match="not the bytes"):
        SkillRegistry.from_revision(_store(store_root, required=True), TENANT, revision)


def test_a_file_changed_between_verification_and_parse_is_not_served(
    example_settings: Settings, store_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    revision = _build(example_settings, store_root, signed=False)
    _changed_after_verification(monkeypatch, store_root)
    registry = SkillRegistry.from_revision(_store(store_root), TENANT, revision)
    assert registry.get("runbook.service_restart") is None
    assert registry.get("runbook.service_health") is not None
    assert [issue.code for issue in registry.parse_errors()] == ["revision_file_mismatch"]


def test_a_policy_changed_after_its_revision_is_loaded_is_refused_and_its_write_denied(
    example_settings: Settings, store_root: Path, tmp_path: Path
) -> None:
    revision = _build(example_settings, store_root)
    catalog = ServedCatalog(_store(store_root, required=True), TENANT, revision)
    skill = catalog.require(RESTART)
    payload = {"service": "checkout"}
    production = {"service": {"environment": "production"}}
    staging = {"service": {"environment": "staging"}}
    released = PolicyEngine(catalog, WriteLedger(tmp_path / "released.sqlite"))
    assert released.evaluate(skill, "service.restart", payload, staging, "s").effect == "allow"

    policy = skill.path.parent / "policy.json"
    loosened = {"operation": "service.restart", "effect": "allow", "reason": "loosened"}
    policy.write_text(
        json.dumps({"skill_id": RESTART, "default": "deny", "rules": [loosened]}),
        encoding="utf-8",
    )
    engine = PolicyEngine(catalog, WriteLedger(tmp_path / "changed.sqlite"))

    with pytest.raises(PolicyError, match="manifest"):
        engine.load(skill)
    decision = engine.evaluate(skill, "service.restart", payload, production, "s")
    assert decision.effect == "deny"
    assert decision.rule is None
