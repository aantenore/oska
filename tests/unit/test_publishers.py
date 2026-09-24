"""A change request published as a branch, and a pull request opened without a shell.

Every repository here is a temporary one, with the operator's own git
configuration shut out, and no test reaches a network: the push goes to a bare
repository on disk and the pull request command is the interpreter echoing its
arguments back.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

import oska.learning.publishers as publishers
from oska.cli.commands_change import app as change_app
from oska.contracts.proposal import ActorSource, QualityReport
from oska.learning.changes import (
    ChangeRequestError,
    ChangeRequestState,
    ChangeRequestStore,
    new_request,
)
from oska.learning.publishers import (
    CommandOpener,
    DirectoryPublisher,
    GitPublisher,
    publisher_for,
)

MARKDOWN = "---\nid: runbook.sample\n---\n\n## Steps\n\n1. Do it.\n"
POLICY = {"skill_id": "runbook.sample", "default": "deny", "rules": []}
ACTOR = "Ada Reviewer <ada@example.org>"
ECHO = [sys.executable, "-c", "import json, sys; print(json.dumps(sys.argv[1:]))"]
runner = CliRunner()


@pytest.fixture(autouse=True)
def _hermetic_git(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Git as a fresh installation sees it: no global or system configuration."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-global-config"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    """A repository with one commit on `main` and a catalog directory in it."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.name", "Operator")
    _git(root, "config", "user.email", "operator@example.org")
    (root / "skills").mkdir()
    (root / "skills" / "README.md").write_text("catalog\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "initial")
    return root


@pytest.fixture
def store(tmp_path: Path) -> ChangeRequestStore:
    return ChangeRequestStore(tmp_path / "changes")


def _saved(
    store: ChangeRequestStore,
    *,
    version: int = 1,
    actor_source: ActorSource = ActorSource.principal,
) -> str:
    request = new_request(
        "proposal-1",
        "runbook.sample",
        version,
        ACTOR,
        QualityReport(issues=[], score=1.0),
        actor_source=actor_source,
    )
    store.save(request, skill_markdown=MARKDOWN, policy=POLICY)
    return request.request_id


def test_the_directory_publisher_leaves_the_request_as_it_was(
    store: ChangeRequestStore,
) -> None:
    request_id = _saved(store)
    published = DirectoryPublisher().publish(store.get(request_id), store)
    assert published.publisher == "directory"
    assert published.branch == ""
    assert publisher_for("directory").name == "directory"


def test_the_git_publisher_makes_a_branch_with_the_package_as_the_actor(
    store: ChangeRequestStore, repository: Path
) -> None:
    request_id = _saved(store)
    head = _git(repository, "rev-parse", "HEAD")

    published = GitPublisher(repository, catalog_path="skills").publish(
        store.get(request_id), store
    )

    assert published.branch == f"change/{request_id}"
    assert published.commit == _git(repository, "rev-parse", published.branch)
    assert published.pushed is False
    held = store.get(request_id)
    assert held.branch == published.branch
    assert held.commit == published.commit
    assert held.state is ChangeRequestState.open
    assert held.publisher == "git"
    author = _git(repository, "log", "-1", "--format=%an <%ae>", published.branch)
    assert author == ACTOR
    files = _git(repository, "ls-tree", "-r", "--name-only", published.branch).splitlines()
    assert "skills/runbook/sample/v1/skill.md" in files
    assert "skills/runbook/sample/v1/policy.json" in files
    shown = _git(repository, "show", f"{published.branch}:skills/runbook/sample/v1/skill.md")
    assert shown == MARKDOWN.strip()
    assert _git(repository, "rev-parse", "HEAD") == head
    assert _git(repository, "status", "--porcelain") == ""
    assert _git(repository, "worktree", "list").count("\n") == 0
    assert "Branch: change/" in (store.directory(request_id) / "summary.md").read_text()


def test_nothing_is_pushed_unless_asked(
    store: ChangeRequestStore, repository: Path, tmp_path: Path
) -> None:
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", str(remote))
    _git(repository, "remote", "add", "origin", str(remote))

    quiet = GitPublisher(repository).publish(store.get(_saved(store)), store)
    assert _git(remote, "branch", "--list") == ""
    assert quiet.pushed is False

    pushed = GitPublisher(repository, push=True).publish(
        store.get(_saved(store, version=2)), store
    )
    assert pushed.pushed is True
    assert pushed.branch in _git(remote, "branch", "--list")
    assert quiet.branch not in _git(remote, "branch", "--list")


def test_the_git_publisher_never_forces_and_never_merges(
    store: ChangeRequestStore,
    repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OSKA-EVO-38: every git command the publisher runs, over a full publish and
    push, carries no `--force` (or `-f`) flag and never invokes `merge`."""
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", str(remote))
    _git(repository, "remote", "add", "origin", str(remote))

    recorded: list[list[str]] = []
    real_run = subprocess.run

    def _spy(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        recorded.append(list(argv))
        return real_run(argv, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(publishers.subprocess, "run", _spy)

    published = GitPublisher(repository, push=True).publish(store.get(_saved(store)), store)

    assert published.pushed is True
    assert recorded, "the publish ran at least one git command"
    for argv in recorded:
        assert "--force" not in argv
        assert "-f" not in argv
        assert "merge" not in argv


def test_a_version_already_on_the_base_is_refused(
    store: ChangeRequestStore, repository: Path
) -> None:
    package = repository / "runbook" / "sample" / "v1"
    package.mkdir(parents=True)
    (package / "skill.md").write_text(MARKDOWN, encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-q", "-m", "a version already there")
    request_id = _saved(store)
    with pytest.raises(ChangeRequestError, match="never overwritten"):
        GitPublisher(repository).publish(store.get(request_id), store)
    assert store.get(request_id).branch == ""


def test_a_request_is_published_as_a_branch_once(
    store: ChangeRequestStore, repository: Path
) -> None:
    request_id = _saved(store)
    GitPublisher(repository).publish(store.get(request_id), store)
    with pytest.raises(ChangeRequestError, match="already on branch"):
        GitPublisher(repository).publish(store.get(request_id), store)


def test_a_pull_request_needs_a_pushed_branch(repository: Path) -> None:
    with pytest.raises(ChangeRequestError, match="pushed"):
        GitPublisher(repository, opener=CommandOpener(ECHO))


def test_the_command_opener_substitutes_into_argv_and_never_uses_a_shell(
    tmp_path: Path,
) -> None:
    seen: dict = {}

    def runner_(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen["argv"], seen["kwargs"] = argv, kwargs
        return subprocess.CompletedProcess(argv, 0, stdout="opened\n", stderr="")

    title = "runbook.sample v1; rm -rf /"
    opener = CommandOpener(
        ["provider", "pr", "create", "--head", "{branch}", "--title", "{title}"],
        runner=runner_,
    )
    answer = opener.open(branch="change/abc", title=title, cwd=tmp_path)
    assert answer == "opened"
    assert seen["argv"] == ["provider", "pr", "create", "--head", "change/abc", "--title", title]
    assert seen["kwargs"]["shell"] is False


def test_the_command_opener_runs_for_real_and_its_answer_is_recorded(
    store: ChangeRequestStore, repository: Path, tmp_path: Path
) -> None:
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", str(remote))
    _git(repository, "remote", "add", "origin", str(remote))
    opener = CommandOpener([*ECHO, "{branch}", "$(whoami) {title}"])
    request_id = _saved(store)
    published = GitPublisher(repository, push=True, opener=opener).publish(
        store.get(request_id), store
    )
    assert json.loads(published.pull_request) == [
        published.branch,
        "$(whoami) runbook.sample v1",
    ]
    assert store.get(request_id).pull_request == published.pull_request


def test_a_failing_command_is_reported(tmp_path: Path) -> None:
    failing = CommandOpener([sys.executable, "-c", "import sys; sys.exit('no provider')"])
    with pytest.raises(ChangeRequestError, match="no provider"):
        failing.open(branch="b", title="t", cwd=tmp_path)


def test_the_cli_publishes_a_request_as_a_branch(
    store: ChangeRequestStore, repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("oska.cli.commands_change.store_for", lambda settings: store)
    request_id = _saved(store)
    result = runner.invoke(
        change_app, ["publish", request_id, "--repo", str(repository), "--path", "skills"]
    )
    assert result.exit_code == 0, result.output
    assert f"change/{request_id}" in result.output
    assert "not pushed" in result.output
    again = runner.invoke(change_app, ["publish", request_id, "--repo", str(repository)])
    assert again.exit_code == 1


def test_the_cli_publishes_a_stated_approver_only_when_the_operator_allows_it(
    store: ChangeRequestStore, repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("oska.cli.commands_change.store_for", lambda settings: store)
    request_id = _saved(store, actor_source=ActorSource.stated)

    refused = runner.invoke(change_app, ["publish", request_id, "--repo", str(repository)])
    assert refused.exit_code == 1
    assert "not verified" in refused.output
    assert store.get(request_id).branch == ""

    allowed = runner.invoke(
        change_app,
        ["publish", request_id, "--repo", str(repository), "--allow-unverified-actor"],
    )
    assert allowed.exit_code == 0, allowed.output
    branch = store.get(request_id).branch
    assert "(stated, not verified)" in _git(repository, "log", "-1", "--format=%B", branch)
