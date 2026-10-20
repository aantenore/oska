"""Where a change request is published for review: a directory, or a branch.

A change request is always written to the change request store; that record
is what every decision is read from, whether the store is a directory or a
database every replica shares. Publishing it is the step that puts the
package in front of the person who reviews it, and there are two ways to do so.

`directory` is the record itself: the package is already there, and a person
reads it and applies it with `oska change apply`. `git` writes the package into
a new branch of a repository the operator names, laid out under the catalog
path of that repository, in one commit authored by the actor who approved the
publication. The branch is made in a worktree of its own, so the checkout the
operator works in is never touched, and the worktree is removed once the commit
is made. Nothing is pushed unless the caller asks, nothing is ever forced, and a
version already present on the base is refused, as `apply` refuses it.

A commit is authored as the actor, so the actor must be one the runtime did not
take on a caller's word. A request whose actor is only stated is refused unless
the publisher is built with `allow_unverified_actor`, which the operator passes
explicitly from the command line; the commit message then says the name was
stated and not verified.

A pull request is a hosting provider's notion and stays outside the runtime.
`CommandOpener` runs a command the operator configures, with the branch and the
title substituted into its arguments, and never through a shell.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from oska.contracts.stores import ChangeRequestRecordStore
from oska.learning.changes import (
    ChangeRequest,
    ChangeRequestError,
    ChangeRequestState,
)

__all__ = [
    "BRANCH_PLACEHOLDER",
    "DEFAULT_REMOTE",
    "TITLE_PLACEHOLDER",
    "ChangePublisher",
    "CommandOpener",
    "DirectoryPublisher",
    "GitPublisher",
    "PullRequestOpener",
    "publisher_for",
    "publisher_names",
]

BRANCH_PLACEHOLDER = "{branch}"
TITLE_PLACEHOLDER = "{title}"
DEFAULT_BRANCH_PREFIX = "change/"
DEFAULT_REMOTE = "origin"
DEFAULT_TIMEOUT_SECONDS = 120.0


class ChangePublisher(Protocol):
    """What puts a composed change request in front of the person who reviews it."""

    @property
    def name(self) -> str:
        """The name this publisher is selected by and recorded under."""
        ...

    def publish(
        self, request: ChangeRequest, store: ChangeRequestRecordStore
    ) -> ChangeRequest:
        """Publish one open request whose package the store holds, and record how."""
        ...


class PullRequestOpener(Protocol):
    """What asks a hosting provider to open a pull request for a pushed branch."""

    def open(self, *, branch: str, title: str, cwd: Path) -> str:
        """Open one, and return what the provider answered, such as its address."""
        ...


class DirectoryPublisher:
    """The change request root is the publication: there is nothing more to write."""

    name = "directory"

    def publish(
        self, request: ChangeRequest, store: ChangeRequestRecordStore
    ) -> ChangeRequest:
        """Return the request as it is, once the store is known to hold its package."""
        if not store.files(request.request_id):
            raise ChangeRequestError(f"change request {request.request_id} holds no package")
        return request


class CommandOpener:
    """Open a pull request by running a command the operator configured.

    The template is an argument vector. `{branch}` and `{title}` are replaced
    inside each argument, and the result is run directly, never through a
    shell, so a title cannot become a second command.
    """

    def __init__(
        self,
        template: Sequence[str],
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        if not template:
            raise ValueError("a pull request command needs at least a program to run")
        self._template = list(template)
        self._timeout = timeout_seconds
        self._run = runner

    def argv(self, *, branch: str, title: str) -> list[str]:
        """The argument vector this template gives for one branch and title."""
        return [
            part.replace(BRANCH_PLACEHOLDER, branch).replace(TITLE_PLACEHOLDER, title)
            for part in self._template
        ]

    def open(self, *, branch: str, title: str, cwd: Path) -> str:
        """Run the command and return what it printed, or raise saying why it failed."""
        argv = self.argv(branch=branch, title=title)
        try:
            done = self._run(
                argv,
                cwd=str(cwd),
                shell=False,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ChangeRequestError(f"the pull request command did not run: {exc}") from exc
        if done.returncode != 0:
            raise ChangeRequestError(
                f"the pull request command exited with {done.returncode}: "
                f"{_last_line(done.stderr)}"
            )
        return done.stdout.strip()


class GitPublisher:
    """Publish a change request as one commit on a new branch of a git repository."""

    name = "git"

    def __init__(
        self,
        repository: Path | str,
        *,
        catalog_path: str = "",
        base: str = "HEAD",
        branch_prefix: str = DEFAULT_BRANCH_PREFIX,
        push: bool = False,
        remote: str = DEFAULT_REMOTE,
        opener: PullRequestOpener | None = None,
        git: str = "git",
        allow_unverified_actor: bool = False,
    ) -> None:
        if opener is not None and not push:
            raise ChangeRequestError(
                "a pull request is opened for a pushed branch; ask for the push as well"
            )
        self._repository = Path(repository)
        self._catalog_path = catalog_path.strip("/")
        self._base = base
        self._prefix = branch_prefix
        self._push = push
        self._remote = remote
        self._opener = opener
        self._git = git
        self._allow_unverified = allow_unverified_actor

    def publish(
        self, request: ChangeRequest, store: ChangeRequestRecordStore
    ) -> ChangeRequest:
        """Write the package into a new branch, commit it as the actor, and record it.

        The branch and the commit are recorded as soon as the commit exists, so
        a push or a pull request that fails afterwards leaves a request that
        says where its branch is.
        """
        if request.state is not ChangeRequestState.open:
            raise ChangeRequestError(
                f"change request {request.request_id} is {request.state.value}, not open"
            )
        if request.branch:
            raise ChangeRequestError(
                f"change request {request.request_id} is already on branch {request.branch}"
            )
        if not request.actor_verified and not self._allow_unverified:
            raise ChangeRequestError(
                f"change request {request.request_id} names an approver that was stated, "
                "not verified; a commit is authored as a verified actor only, unless the "
                "operator allows an unverified one explicitly"
            )
        if not store.files(request.request_id):
            raise ChangeRequestError(f"change request {request.request_id} holds no package")
        repository = self._repository.resolve()
        self._run(repository, "rev-parse", "--git-dir")
        branch = f"{self._prefix}{request.request_id}"
        relative = "/".join(part for part in (self._catalog_path, request.package) if part)
        if self._run(repository, "ls-tree", "--name-only", self._base, "--", relative):
            raise ChangeRequestError(
                f"{relative} is already on {self._base}: a version is never overwritten"
            )
        request.commit = self._commit(repository, branch, relative, store, request)
        request.publisher = self.name
        request.repository = str(repository)
        request.branch = branch
        store.save(request)
        if self._push:
            self._run(repository, "push", self._remote, f"{branch}:{branch}")
            request.pushed = True
            store.save(request)
        if self._opener is not None:
            title = f"{request.skill_id} v{request.version}"
            request.pull_request = self._opener.open(branch=branch, title=title, cwd=repository)
            store.save(request)
        return request

    def _commit(
        self,
        repository: Path,
        branch: str,
        relative: str,
        store: ChangeRequestRecordStore,
        request: ChangeRequest,
    ) -> str:
        """Make the branch in a worktree of its own, commit the package, and remove the tree."""
        worktree = Path(tempfile.mkdtemp(prefix="oska-change-"))
        self._run(repository, "worktree", "add", "-b", branch, str(worktree), self._base)
        try:
            store.write_package(request.request_id, worktree / relative)
            self._run(worktree, "add", "--", relative)
            self._run(
                worktree,
                "commit",
                "-m",
                _message(request),
                env=_as_author(request.actor),
            )
            return self._run(worktree, "rev-parse", "HEAD")
        finally:
            self._remove(repository, worktree)

    def _remove(self, repository: Path, worktree: Path) -> None:
        """Remove the worktree; a tree left dirty by a failure is deleted and pruned."""
        try:
            self._run(repository, "worktree", "remove", str(worktree))
        except ChangeRequestError:
            shutil.rmtree(worktree, ignore_errors=True)
            try:
                self._run(repository, "worktree", "prune")
            except ChangeRequestError:
                pass

    def _run(self, cwd: Path, *args: str, env: Mapping[str, str] | None = None) -> str:
        argv = [self._git, "-C", str(cwd), *args]
        merged = None if env is None else {**os.environ, **env}
        try:
            done = subprocess.run(
                argv, shell=False, capture_output=True, text=True, check=False, env=merged
            )
        except OSError as exc:
            raise ChangeRequestError(f"git could not be run: {exc}") from exc
        if done.returncode != 0:
            raise ChangeRequestError(f"git {args[0]} failed: {_last_line(done.stderr)}")
        return done.stdout.strip()


PUBLISHERS: dict[str, Callable[..., ChangePublisher]] = {
    DirectoryPublisher.name: lambda **_: DirectoryPublisher(),
    GitPublisher.name: GitPublisher,
}


def publisher_names() -> list[str]:
    """Every publisher a caller may select by name."""
    return sorted(PUBLISHERS)


def publisher_for(name: str, **options: Any) -> ChangePublisher:
    """The publisher registered under a name, built with the options it takes."""
    factory = PUBLISHERS.get(name)
    if factory is None:
        raise ChangeRequestError(
            f"no publisher is called {name!r}; known: {', '.join(publisher_names())}"
        )
    return factory(**options)


def _as_author(actor: str) -> dict[str, str]:
    """Author and commit as the actor. `Name <address>` is split; a bare name has none."""
    name, _, rest = actor.partition("<")
    address = rest.rstrip(">").strip() if rest else ""
    name = name.strip() or actor.strip()
    return {
        "GIT_AUTHOR_NAME": name,
        "GIT_AUTHOR_EMAIL": address,
        "GIT_COMMITTER_NAME": name,
        "GIT_COMMITTER_EMAIL": address,
    }


def _message(request: ChangeRequest) -> str:
    """The commit message: what changes first, then where it came from."""
    return "\n".join(
        [
            f"{request.skill_id} v{request.version}",
            "",
            f"Change request {request.request_id}, drafted from proposal "
            f"{request.proposal_id} and approved by {_approver(request)}.",
            f"Quality score {request.quality.score:.2f}.",
            "",
        ]
    )


def _approver(request: ChangeRequest) -> str:
    """Who approved, and whether that name is only what the approving call said."""
    if request.actor_verified:
        return request.actor
    return f"{request.actor} (stated, not verified)"


def _last_line(text: str) -> str:
    lines = [line for line in (text or "").splitlines() if line.strip()]
    return lines[-1].strip() if lines else "no detail"
