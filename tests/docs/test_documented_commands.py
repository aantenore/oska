"""The commands the README and the platform walkthrough show must run.

Both documents say their commands were run. Nothing re-ran them, so they could
rot. This module reads the fenced ``sh``, ``bash`` and ``console`` blocks out of
the markdown itself at test time (no command is copied here), runs the offline
and deterministic ones in order, in a scratch directory, the way a reader would,
and checks two things:

* every command exits zero;
* where an untagged or ``text`` block follows the command block, every line of
  it appears in the real output, with ids, timestamps and the scratch path
  normalized away. A leading ``> `` is dropped from a documented line: the
  interactive prompt goes to the terminal ahead of each reply, and a captured
  run keeps it on a separate stream.

A block is run only when every command in it is on an allowlist of offline
subcommands (``new``, ``pack check``, ``conform``, ``validate``,
``capabilities``, ``ontology``, ``evaluate catalog``, ``run``). The runtime
configuration is the packaged default (no provider), ``OSKA_*`` variables of
the calling environment are dropped, and ``OSKA_STATE_DIR`` points into the
scratch directory. Everything else is skipped by rule, with the reason shown in
the test id and the skip message:

* environment setup: ``git``, ``uv``, ``pip``, ``python``, ``source``, ``set``,
  ``export``, ``cd``, ``.`` (``README.md`` clone/install blocks, the
  ``set -a; . ./.env`` block, ``export`` of keys in the walkthrough);
* needs a server, the network or a real model: ``oska serve``, ``curl``,
  ``pytest`` against PostgreSQL;
* any other command (for example ``oska revision``, which needs the signing
  entry a reader first adds to ``configs/runtime.json`` in a ``json`` block);
* a block marked with ``<!-- doc-test: skip reason -->`` on the line before it.

Working directory: a block runs in the scratch directory, shared by the blocks
of one document so that ``oska new pack`` is there for ``oska pack check``. It
runs in the repository root instead when the section says "From the repository
root", or when a path it names exists only there (the cassette of the first
README block). A section that says "Run from inside `dir/`" runs in that
directory of the scratch directory from then on, until the next heading.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCUMENTS = ("README.md", "docs/building-a-platform.md")
SHELL_LANGS = {"sh", "bash", "console"}
OUTPUT_LANGS = {"", "text"}
SKIP_MARKER = re.compile(r"^<!--\s*doc-test:\s*skip\b\s*(.*?)\s*-->$")
FENCE = re.compile(r"^(```+)\s*([\w-]*)\s*$")
ROOT_HINT = re.compile(r"\bfrom the repository root\b", re.IGNORECASE)
INSIDE_HINT = re.compile(r"\brun from inside `([^`]+)`", re.IGNORECASE)

SETUP_WORDS = {"git", "uv", "pip", "pip3", "python", "python3", "source", "set", "export"}
SETUP_WORDS |= {"cd", "."}
SERVER_WORDS = {"curl", "wget", "pytest"}
OFFLINE_SUBCOMMANDS = {
    ("new",),
    ("pack", "check"),
    ("conform",),
    ("validate",),
    ("capabilities",),
    ("ontology",),
    ("evaluate", "catalog"),
    ("run",),
}
HARMLESS_WORDS = {"printf", "echo"}
PROMPT = "> "
OPERATORS = {"|", "||", "&&", ";", "&"}
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
HEX_ID = re.compile(r"\b[0-9a-f]{16}\b")
TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?")


@dataclass
class Block:
    """One fenced shell block, with the context the runner needs."""

    document: str
    line: int
    lang: str
    body: list[str]
    skip_reason: str | None = None
    cwd_hint: str | None = None
    expected: list[str] = field(default_factory=list)

    @property
    def ident(self) -> str:
        return f"{self.document}:{self.line}"

    @property
    def console_style(self) -> bool:
        return any(line.startswith("$ ") for line in self.body)

    def commands(self) -> list[str]:
        """Logical commands, continuations joined, comments and prompts removed."""
        commands: list[str] = []
        pending = ""
        for raw in self.body:
            line = raw
            if self.console_style:
                if not line.startswith("$ "):
                    continue
                line = line[2:]
            if pending:
                line = pending + line.lstrip()
                pending = ""
            if line.rstrip().endswith("\\"):
                pending = line.rstrip()[:-1] + " "
                continue
            if line.strip() and not line.lstrip().startswith("#"):
                commands.append(line.strip())
        if pending.strip():
            commands.append(pending.strip())
        return commands

    def inline_output(self) -> list[str]:
        """Output lines written inside a ``$`` style block."""
        if not self.console_style:
            return []
        return [line for line in self.body if not line.startswith("$ ") and line.strip()]


def _segments(command: str) -> list[list[str]]:
    """Split a command line into simple commands, each a list of words."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.commenters = "#"
    lexer.whitespace_split = True
    segments: list[list[str]] = [[]]
    for token in lexer:
        if token in OPERATORS:
            segments.append([])
        elif segments[-1] or not ASSIGNMENT.match(token):
            segments[-1].append(token)
    return [s for s in segments if s]


def classify(block: Block) -> str | None:
    """Return why a block is not run, or None when it is offline and deterministic."""
    if block.skip_reason is not None:
        return f"marked skip: {block.skip_reason}" if block.skip_reason else "marked skip"
    words: list[tuple[str, ...]] = []
    for command in block.commands():
        for segment in _segments(command):
            words.append(tuple(segment))
    if not words:
        return "no command"
    setup = sorted({w[0] for w in words if w[0] in SETUP_WORDS})
    if setup:
        return f"environment setup ({', '.join(setup)})"
    server = sorted({w[0] for w in words if w[0] in SERVER_WORDS})
    server += [" ".join(w[:2]) for w in words if w[0] == "oska" and w[1:2] == ("serve",)]
    if server:
        return f"needs a server, network or a real model ({', '.join(server)})"
    for w in words:
        if w[0] in HARMLESS_WORDS:
            continue
        if w[0] != "oska":
            return f"not an offline command ({w[0]})"
        rest = w[1:]
        if not any(rest[: len(sub)] == sub for sub in OFFLINE_SUBCOMMANDS):
            return f"not an offline command (oska {' '.join(rest[:2])})"
    return None


def parse(document: str) -> list[Block]:
    """Extract the shell blocks of one document, each with its expected output."""
    lines = (REPO_ROOT / document).read_text(encoding="utf-8").splitlines()
    blocks: list[Block] = []
    section_hint: str | None = None
    prose: list[str] = []
    marker: str | None = None
    previous: Block | None = None
    after_command_block = False
    i = 0
    while i < len(lines):
        line = lines[i]
        fence = FENCE.match(line)
        if fence is None:
            stripped = line.strip()
            if line.startswith("#"):
                section_hint = None
                prose = []
                marker = None
                after_command_block = False
            elif stripped:
                skip = SKIP_MARKER.match(stripped)
                if skip:
                    marker = skip.group(1)
                else:
                    marker = None
                    prose.append(stripped)
                    after_command_block = False
            i += 1
            continue
        ticks, lang = fence.groups()
        end = i + 1
        while end < len(lines) and lines[end].strip() != ticks:
            end += 1
        body = lines[i + 1 : end]
        text = " ".join(prose)
        for hint in (ROOT_HINT.search(text), INSIDE_HINT.search(text)):
            if hint is not None:
                section_hint = hint.group(1) if hint.re is INSIDE_HINT else "."
        if lang in SHELL_LANGS:
            block = Block(document, i + 1, lang, body, marker, section_hint)
            block.expected = block.inline_output()
            blocks.append(block)
            previous = block
            after_command_block = True
        elif after_command_block and lang in OUTPUT_LANGS and previous is not None:
            previous.expected.extend(body)
            after_command_block = False
        else:
            after_command_block = False
        prose = []
        marker = None
        i = end + 1
    return blocks


def normalize(text: str, scratch: Path) -> str:
    """Strip what changes between runs: ids, timestamps, the scratch path."""
    text = text.replace(str(scratch.resolve()), "/path/to")
    text = text.replace(str(scratch), "/path/to")
    text = UUID.sub("<id>", text)
    text = TIMESTAMP.sub("<time>", text)
    text = HEX_ID.sub("<id>", text)
    return text


def _wants_repo_root(block: Block, scratch: Path) -> bool:
    """True when a path the block names exists in the checkout and not in the scratch dir."""
    for command in block.commands():
        for token in shlex.split(command, comments=True):
            if "/" not in token or token.startswith(("-", "/", "$")):
                continue
            if (REPO_ROOT / token).exists() and not (scratch / token).exists():
                return True
    return False


def _cwd(block: Block, scratch: Path) -> Path:
    if block.cwd_hint == ".":
        return REPO_ROOT
    if block.cwd_hint:
        return scratch / block.cwd_hint
    if _wants_repo_root(block, scratch):
        return REPO_ROOT
    return scratch


def _environment(shim: Path, state: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("OSKA_")}
    env["PATH"] = f"{shim}{os.pathsep}{env.get('PATH', '')}"
    env["OSKA_STATE_DIR"] = str(state)
    env["COLUMNS"] = "200"
    env["NO_COLOR"] = "1"
    env["TERM"] = "dumb"
    return env


@pytest.fixture(scope="module")
def shim(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A directory whose ``oska`` runs the entry point of the current environment."""
    directory = tmp_path_factory.mktemp("shim")
    script = directory / "oska"
    script.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" -m oska.cli.main "$@"\n', encoding="utf-8"
    )
    script.chmod(0o755)
    return directory


@pytest.fixture(scope="module")
def workspaces(tmp_path_factory: pytest.TempPathFactory) -> dict[str, tuple[Path, Path]]:
    """One scratch directory and state directory per document, shared by its blocks."""
    spaces: dict[str, tuple[Path, Path]] = {}
    for document in DOCUMENTS:
        root = tmp_path_factory.mktemp(Path(document).stem)
        scratch, state = root / "work", root / "state"
        scratch.mkdir()
        state.mkdir()
        spaces[document] = (scratch, state)
    return spaces


def _cases() -> list[object]:
    cases: list[object] = []
    for document in DOCUMENTS:
        for block in parse(document):
            reason = classify(block)
            label = f"{block.ident}-{'skip' if reason else 'run'}"
            mark = [pytest.mark.skip(reason=reason)] if reason else []
            cases.append(pytest.param(block, id=label, marks=mark))
    return cases


def test_documents_have_runnable_blocks() -> None:
    """The extraction finds the blocks it exists for; an empty parse would pass silently."""
    for document in DOCUMENTS:
        blocks = parse(document)
        assert any(classify(b) is None for b in blocks), f"{document}: nothing to run"


@pytest.mark.parametrize("block", _cases())
def test_documented_block(
    block: Block, shim: Path, workspaces: dict[str, tuple[Path, Path]]
) -> None:
    scratch, state = workspaces[block.document]
    env = _environment(shim, state)
    cwd = _cwd(block, scratch)
    cwd.mkdir(parents=True, exist_ok=True)
    output = ""
    for command in block.commands():
        done = subprocess.run(
            ["bash", "-o", "pipefail", "-c", command],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        output += done.stdout + done.stderr
        assert done.returncode == 0, (
            f"{block.ident}: `{command}` exited {done.returncode}\n{done.stdout}{done.stderr}"
        )
    actual = {line.rstrip() for line in normalize(output, scratch).splitlines()}
    missing = [
        line
        for line in normalize("\n".join(block.expected), scratch).splitlines()
        if line.strip() and line.removeprefix(PROMPT).rstrip() not in actual
    ]
    assert not missing, f"{block.ident}: documented output not produced: {missing}\n{output}"
