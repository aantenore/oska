"""The kit: generating a pack or a platform, and checking or building one.

`new_pack` promises that what it writes passes `check_pack` at the level
`pack.yaml` claims, with no placeholder left unsubstituted anywhere in the
tree. `new_platform` promises a manifest that validates and names the packs it
was given. `build_pack` promises a hash that depends on every byte of the pack
and nothing else.
"""

from __future__ import annotations

import json
import os
import re
import tarfile
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

import oska
import oska.platform.runtime as platform_runtime
from oska.cli.commands_new import app as new_app
from oska.cli.commands_pack import app as pack_app
from oska.contracts.platform import PlatformManifest
from oska.kit.build import build_pack
from oska.kit.check import check_pack
from oska.kit.scaffold import new_pack, new_platform
from oska.platform import Platform, load_platform
from oska.platform.conformance import passed, run_platform

runner = CliRunner()


def test_new_pack_passes_check_at_its_claimed_level(tmp_path: Path) -> None:
    target = new_pack("sample_pack", into=tmp_path)
    report = check_pack(target)
    assert report.ok, (report.errors, report.validation_issues)
    assert report.attained_level == report.claimed_level
    assert report.attained_level >= 2


def test_new_pack_writes_no_unsubstituted_placeholder(tmp_path: Path) -> None:
    target = new_pack("sample_pack", entity="widget", system="control", into=tmp_path)
    for path in target.rglob("*"):
        if path.is_file():
            assert "${" not in path.read_text(encoding="utf-8"), path
        assert "${" not in path.name, path


def test_new_pack_uses_the_names_it_is_given(tmp_path: Path) -> None:
    target = new_pack("sample_pack", entity="widget", system="control", into=tmp_path)
    assert (target / "skills" / "entity" / "widget" / "v1" / "skill.md").is_file()
    assert (target / "skills" / "integration" / "control" / "v1" / "skill.md").is_file()
    assert (target / "fixtures" / "widgets.json").is_file()
    manifest = yaml.safe_load((target / "pack.yaml").read_text(encoding="utf-8"))
    assert manifest["provides"]["entities"] == ["entity.widget"]


def test_new_pack_puts_the_right_article_before_its_entity(tmp_path: Path) -> None:
    """An entity starting with a vowel reads "an item", and the pack still conforms."""
    target = new_pack("vowel_pack", into=tmp_path, entity="item")
    text = "\n".join(
        path.read_text(encoding="utf-8") for path in target.rglob("*") if path.is_file()
    )
    assert "an item" in text
    assert "a item" not in text
    report = check_pack(target)
    assert report.ok, (report.errors, report.validation_issues)


def test_new_pack_refuses_to_overwrite(tmp_path: Path) -> None:
    new_pack("sample_pack", into=tmp_path)
    with pytest.raises(FileExistsError):
        new_pack("sample_pack", into=tmp_path)


def test_new_pack_rejects_a_bad_name(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        new_pack("Not A Name", into=tmp_path)


def test_new_platform_manifest_validates_and_names_the_pack(tmp_path: Path) -> None:
    pack_dir = new_pack("sample_pack", into=tmp_path)
    platform_dir = new_platform("sample_platform", packs=[pack_dir], into=tmp_path)
    document = yaml.safe_load((platform_dir / "platform.yaml").read_text(encoding="utf-8"))
    manifest = PlatformManifest.model_validate(document)
    assert manifest.name == "sample_platform"
    assert "sample_pack" in manifest.packs
    assert manifest.domains[0].packs == ["sample_pack"]
    assert (platform_dir / "packs" / "sample_pack" / "pack.yaml").is_file()
    assert (platform_dir / "configs" / "runtime.json").is_file()
    assert (platform_dir / "configs" / "guardrails.json").is_file()


def test_new_platform_ci_installs_the_version_that_generated_it(tmp_path: Path) -> None:
    pack_dir = new_pack("sample_pack", into=tmp_path)
    platform_dir = new_platform("sample_platform", packs=[pack_dir], into=tmp_path)
    workflow = platform_dir / ".github" / "workflows" / "conformance.yml"
    text = workflow.read_text(encoding="utf-8")
    assert f"'oska=={oska.__version__}'" in text
    assert "vars.OSKA_INSTALL" in text
    assert "${oska_version}" not in text
    assert not re.search(r"pip install\s+\"?oska\"?\s*$", text, re.MULTILINE)
    readme = (platform_dir / "README.md").read_text(encoding="utf-8")
    assert f"oska=={oska.__version__}" in readme
    assert "${oska_version}" not in readme
    assert not re.search(r"pip install\s+oska\b", readme)
    install = yaml.safe_load(text)["jobs"]["conform"]["steps"][2]
    assert install["run"] == 'pip install "$OSKA_INSTALL"'


def test_new_platform_refuses_to_overwrite(tmp_path: Path) -> None:
    pack_dir = new_pack("sample_pack", into=tmp_path)
    new_platform("sample_platform", packs=[pack_dir], into=tmp_path)
    with pytest.raises(FileExistsError):
        new_platform("sample_platform", packs=[pack_dir], into=tmp_path)


def test_build_pack_is_deterministic(tmp_path: Path) -> None:
    pack_dir = new_pack("sample_pack", into=tmp_path)
    first = build_pack(pack_dir, tmp_path / "dist1")
    second = build_pack(pack_dir, tmp_path / "dist2")
    assert first.content_hash == second.content_hash
    assert first.files == second.files


def test_build_pack_changes_when_one_byte_changes(tmp_path: Path) -> None:
    pack_dir = new_pack("sample_pack", into=tmp_path)
    before = build_pack(pack_dir, tmp_path / "dist1")
    skill = pack_dir / "skills" / "entity" / "item" / "v1" / "skill.md"
    skill.write_text(skill.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    after = build_pack(pack_dir, tmp_path / "dist2")
    assert before.content_hash != after.content_hash


def test_cli_new_pack_writes_a_conformant_pack(tmp_path: Path) -> None:
    result = runner.invoke(new_app, ["pack", "cli_pack", "--into", str(tmp_path)])
    assert result.exit_code == 0, result.output
    report = check_pack(tmp_path / "cli_pack")
    assert report.ok


def test_cli_new_pack_refuses_to_overwrite(tmp_path: Path) -> None:
    runner.invoke(new_app, ["pack", "cli_pack", "--into", str(tmp_path)])
    result = runner.invoke(new_app, ["pack", "cli_pack", "--into", str(tmp_path)])
    assert result.exit_code != 0


def test_cli_new_platform_writes_a_valid_manifest(tmp_path: Path) -> None:
    runner.invoke(new_app, ["pack", "cli_pack", "--into", str(tmp_path)])
    result = runner.invoke(
        new_app,
        [
            "platform",
            "cli_platform",
            "--pack",
            str(tmp_path / "cli_pack"),
            "--into",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.output
    document = yaml.safe_load(
        (tmp_path / "cli_platform" / "platform.yaml").read_text(encoding="utf-8")
    )
    PlatformManifest.model_validate(document)


def test_cli_pack_check_reports_ok_and_exits_zero(tmp_path: Path) -> None:
    target = new_pack("sample_pack", into=tmp_path)
    result = runner.invoke(pack_app, ["check", str(target), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True


def test_cli_pack_check_fails_on_a_broken_pack(tmp_path: Path) -> None:
    target = new_pack("sample_pack", into=tmp_path)
    (target / "skills" / "runbook" / "item_archive" / "v1" / "policy.json").unlink()
    result = runner.invoke(pack_app, ["check", str(target)])
    assert result.exit_code != 0


def test_cli_pack_build_writes_an_archive(tmp_path: Path) -> None:
    target = new_pack("sample_pack", into=tmp_path)
    result = runner.invoke(pack_app, ["build", str(target), "--into", str(tmp_path / "dist")])
    assert result.exit_code == 0, result.output
    archives = list((tmp_path / "dist").glob("*.tar.gz"))
    assert len(archives) == 1


# A third-party pack is checked the way a platform serves it


def _bind(target: Path, integration_id: str, binding: dict) -> None:
    path = target / "configs" / "connectors.json"
    bindings = json.loads(path.read_text(encoding="utf-8"))
    bindings["integrations"][integration_id] = binding
    path.write_text(json.dumps(bindings), encoding="utf-8")


def test_pack_check_refuses_a_shell_binding_unless_allowed(tmp_path: Path) -> None:
    target = new_pack("sample_pack", into=tmp_path)
    marker = tmp_path / "ran"
    command = {"argv": ["touch", str(marker)]}
    _bind(
        target,
        "integration.core",
        {"type": "shell", "operations": {"item.get": command, "item.archive": command}},
    )

    report = check_pack(target)

    assert not report.ok
    assert any("shell" in error and "--allow-shell" in error for error in report.errors)
    assert not marker.exists(), "a refused shell binding ran"
    result = runner.invoke(pack_app, ["check", str(target)])
    assert result.exit_code != 0
    assert "--allow-shell" in result.output
    allowed = check_pack(target, allow_shell=True)
    assert not any("--allow-shell" in error for error in allowed.errors)
    assert marker.exists(), "an allowed shell binding did not run"
    allowed_cli = runner.invoke(pack_app, ["check", str(target), "--allow-shell"])
    assert "--allow-shell" not in allowed_cli.output


def test_pack_check_reads_a_mock_fixture_only_from_inside_the_pack(tmp_path: Path) -> None:
    target = new_pack("sample_pack", into=tmp_path)
    outside = tmp_path / "outside.json"
    outside.write_text((target / "fixtures" / "items.json").read_text(encoding="utf-8"))
    bindings = json.loads((target / "configs" / "connectors.json").read_text(encoding="utf-8"))
    binding = bindings["integrations"]["integration.core"]
    _bind(target, "integration.core", {**binding, "fixture": str(outside)})

    report = check_pack(target)

    assert not report.ok
    assert any("outside the pack" in error for error in report.errors), report.errors


def test_pack_check_builds_the_engine_a_domain_of_that_pack_builds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = new_pack("sample_pack", into=tmp_path)
    (target / "guardrails.json").write_text(
        json.dumps(
            {
                "guardrails": {
                    "guardrail.sample_reviewed": {
                        "title": "Archives are stated plainly",
                        "description": "The reply names only what the read-back confirmed.",
                        "enforcement": {
                            "point": "output",
                            "mechanism": "instruction",
                            "module": "oska.engine.reply",
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    skill = target / "skills" / "runbook" / "item_archive" / "v1" / "skill.md"
    skill.write_text(
        skill.read_text(encoding="utf-8").replace(
            "guardrails:\n", "guardrails:\n  - guardrail.sample_reviewed\n", 1
        ),
        encoding="utf-8",
    )
    manifest = yaml.safe_load((target / "pack.yaml").read_text(encoding="utf-8"))
    manifest["synonyms"] = {"thing": "item"}
    (target / "pack.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    (target / "phrases.json").write_text(
        json.dumps({"it": {"approval_head": "Questa archiviazione va approvata."}}),
        encoding="utf-8",
    )
    built: list[dict[str, Any]] = []
    real = platform_runtime.build_engine

    def spy(settings: Any, **kwargs: Any) -> Any:
        built.append(kwargs)
        return real(settings, **kwargs)

    monkeypatch.setattr(platform_runtime, "build_engine", spy)

    report = check_pack(target)

    assert report.ok, (report.errors, report.validation_issues)
    assert built, "the check did not build its engines through the platform assembly"
    for kwargs in built:
        assert kwargs["guardrails"].get("guardrail.sample_reviewed") is not None
        assert kwargs["synonyms"] == {"thing": "item"}
        assert kwargs["host"].name == manifest["host"]["name"]
        assert kwargs["phrase_layers"] == ((target / "phrases.json").resolve(),)


def test_pack_check_refuses_a_phrase_naming_other_placeholders(tmp_path: Path) -> None:
    target = new_pack("sample_pack", into=tmp_path)
    (target / "phrases.json").write_text(
        json.dumps({"it": {"approval_tail": "Rispondi {yes} per approvarla."}}),
        encoding="utf-8",
    )

    report = check_pack(target)

    assert not report.ok
    refused = [error for error in report.errors if "approval_tail" in error]
    assert refused, report.errors
    assert refused[0].startswith("domain sample_pack: pack sample_pack: ")
    assert "{yes}" in refused[0]


# What a built pack holds


def test_build_pack_leaves_out_what_is_never_shipped(tmp_path: Path) -> None:
    target = new_pack("sample_pack", into=tmp_path)
    (target / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    (target / ".env.local").write_text("TOKEN=secret\n", encoding="utf-8")
    for leftover in ("__pycache__/cached.cpython-311.pyc", ".git/HEAD", "state/sessions.sqlite",
                     "skills/stray.pyc"):
        path = target / leftover
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")

    artifact = build_pack(target, tmp_path / "dist")

    shipped = set(artifact.files)
    assert "pack.yaml" in shipped
    assert not {
        name
        for name in shipped
        if name.startswith((".env", "__pycache__/", ".git/", "state/")) or name.endswith(".pyc")
    }
    with tarfile.open(artifact.archive_path) as archive:
        members = [member.name for member in archive.getmembers()]
    assert not [name for name in members if "/.env" in name or "state/" in name]


def test_build_pack_refuses_a_symbolic_link(tmp_path: Path) -> None:
    target = new_pack("sample_pack", into=tmp_path)
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    os.symlink(outside, target / "fixtures" / "linked.json")

    with pytest.raises(ValueError, match="symbolic link"):
        build_pack(target, tmp_path / "dist")


# A pack written inside a platform joins it


def test_new_pack_registers_itself_in_a_platform_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform_settings: Any
) -> None:
    platform_dir = new_platform("sample_platform", into=tmp_path)
    manifest_path = platform_dir / "platform.yaml"

    result = runner.invoke(
        new_app,
        [
            "pack",
            "first_pack",
            "--into",
            str(platform_dir / "packs"),
            "--platform",
            str(manifest_path),
        ],
    )

    assert result.exit_code == 0, result.output
    manifest = load_platform(manifest_path)
    assert manifest.packs["first_pack"].path == (platform_dir / "packs" / "first_pack").resolve()
    assert manifest.domain().packs == ["first_pack"]
    assert "path: packs/first_pack" in manifest_path.read_text(encoding="utf-8")
    held = Platform.from_file(manifest_path, platform_settings)
    assert passed(held, run_platform(held))

    monkeypatch.chdir(platform_dir)
    result = runner.invoke(
        new_app, ["pack", "second_pack", "--entity", "widget", "--system", "control",
                  "--into", "packs"],
    )

    assert result.exit_code == 0, result.output
    assert load_platform(manifest_path).domain().packs == ["first_pack", "second_pack"]


# First-time safety: what a generated platform says about who can talk to it


def test_the_generated_readme_says_who_can_talk_to_the_platform(tmp_path: Path) -> None:
    pack_dir = new_pack("sample_pack", into=tmp_path)
    platform_dir = new_platform("sample_platform", packs=[pack_dir], into=tmp_path)
    runtime = json.loads((platform_dir / "configs" / "runtime.json").read_text(encoding="utf-8"))
    readme = " ".join((platform_dir / "README.md").read_text(encoding="utf-8").split())

    assert runtime["identity"]["mode"] == "proxy"
    assert not runtime["identity"].get("required", False)
    assert runtime["identity"]["single_user"] is True
    assert "## Who can talk to it" in readme
    assert "This platform starts as a single-user deployment." in readme
    assert (
        "Anyone who can reach the port `oska serve` listens on is this deployment's "
        "one user, reviewer included."
    ) in readme
    assert "Before this deployment serves more than one person:" in readme
    assert "Remove `identity.single_user`" in readme
    assert 'set `"identity": {"mode": "token"}`' in readme
    assert "`OSKA_SESSION_TOKEN_KEY`" in readme
    assert "`Authorization: Bearer <token>`" in readme


def _serve(monkeypatch: pytest.MonkeyPatch, *arguments: str) -> tuple[Any, list[dict]]:
    """Invoke `oska serve` with the application and the server stubbed out."""
    import typer
    import uvicorn

    import oska.api.app as api_app
    from oska.cli.commands_serve import register

    served: list[dict] = []
    monkeypatch.setattr(api_app, "create_app", lambda *_, **__: object())
    monkeypatch.setattr(uvicorn, "run", lambda _app, **options: served.append(options))
    app = typer.Typer()
    register(app)
    skills = Path(__file__).resolve().parents[2] / "examples" / "ops" / "skills"
    result = CliRunner().invoke(app, ["serve", "--skills", str(skills), *arguments])
    return result, served


def test_serve_warns_when_it_listens_beyond_loopback_without_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, served = _serve(monkeypatch, "--host", "0.0.0.0", "--port", "8123")

    assert result.exit_code == 0, result.output
    assert served == [{"host": "0.0.0.0", "port": 8123}]
    assert "warning: listening on 0.0.0.0:8123 with identity.required off" in result.stderr
    assert "identity.mode" in result.stderr


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_serve_says_nothing_on_a_loopback_address(
    monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    result, served = _serve(monkeypatch, "--host", host)

    assert result.exit_code == 0, result.output
    assert served and served[0]["host"] == host
    assert "warning" not in result.stderr
