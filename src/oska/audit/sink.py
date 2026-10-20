"""Where audit events go.

A sink takes an event and is done with it. Nothing downstream may change what
the runtime does, so a sink returns nothing and the runtime never reads back
what it wrote.

The file the events land in is append-only while the runtime is up. The one
thing that removes lines from it is retention, and it does so by writing a new
file beside the old one and renaming it over the top: a trail half-rewritten by
an interrupted process is worse than one that still holds what should have gone,
so the replacement is never partially visible.

`JsonlSink` is also the file-backed audit store: what the report, the metrics,
the improvement job and retention read and remove goes through the same
interface a database-backed trail answers (`oska.storage.AuditStore`), so a
reader is handed either and does not ask which.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from oska.contracts.events import AuditEvent

if TYPE_CHECKING:
    from oska.storage.protocols import AuditStore


@runtime_checkable
class AuditSink(Protocol):
    """Anything that accepts audit events."""

    def emit(self, event: AuditEvent) -> None:
        """Take one event."""
        ...


class MemorySink:
    """Keeps events in a list. Used by tests and by the turn result."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def emit(self, event: AuditEvent) -> None:
        """Append the event."""
        self.events.append(event)

    def clear(self) -> None:
        """Drop everything held."""
        self.events.clear()


class JsonlSink:
    """Appends one JSON object per line to a file."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        """File this sink appends to."""
        return self._path

    @property
    def location(self) -> str:
        """The file, as a person finds it."""
        return str(self._path)

    def emit(self, event: AuditEvent) -> None:
        """Append the event as a single line."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(event.model_dump_json() + "\n")

    def events(self) -> list[AuditEvent]:
        """Every audit event of the file, in the order it was appended.

        A line that is not a readable event is skipped: a trail is append-only
        and a half-written last line must not stop the rest from being read.
        """
        if not self._path.is_file():
            return []
        events: list[AuditEvent] = []
        with self._path.open(encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    events.append(AuditEvent.model_validate_json(stripped))
                except ValueError:
                    continue
        return events

    def exists(self) -> bool:
        """Whether the file is there."""
        return self._path.is_file()

    def delete_sessions(
        self, session_ids: Iterable[str], *, keep_kinds: Iterable[str] = ()
    ) -> dict[str, int]:
        """Rewrite the file without these sessions' events, keeping the kinds named."""
        targets = set(session_ids)
        kept = set(keep_kinds)
        dropped: dict[str, int] = {}

        def keep(entry: dict) -> bool:
            held = entry.get("session_id")
            if held in targets and entry.get("kind") not in kept:
                dropped[held] = dropped.get(held, 0) + 1
                return False
            return True

        if targets:
            rewrite_jsonl(self._path, keep)
        return dropped

    def purge(self) -> int:
        """Remove nothing: the namespace of a file trail is its file, which is not its to remove."""
        return 0


class MultiSink:
    """Passes every event to each of the sinks it holds, in order."""

    def __init__(self, *sinks: AuditSink) -> None:
        self.sinks: list[AuditSink] = list(sinks)

    def emit(self, event: AuditEvent) -> None:
        """Hand the event to every sink."""
        for sink in self.sinks:
            sink.emit(event)


def audit_trail(source: Path | str | AuditStore) -> AuditStore:
    """The audit store a caller named: itself, or the file trail at a path."""
    if isinstance(source, (str, Path)):
        return JsonlSink(Path(source))
    return source


def rewrite_jsonl(path: Path, keep: Callable[[dict], bool]) -> int:
    """Rewrite a trail with only the entries `keep` accepts, and report what went.

    A line that cannot be read as an object is kept: this is an account of what
    happened, and nothing here decides that an entry it does not understand may
    be dropped. The new file is written beside the old one and renamed over it,
    so a reader sees either the whole trail or the whole of its replacement.
    """
    path = Path(path)
    if not path.is_file():
        return 0
    removed = 0
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f"{path.name}.", suffix=".tmp", delete=False
    )
    replacement = Path(handle.name)
    try:
        with handle, path.open("r", encoding="utf-8") as source:
            for line in source:
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    handle.write(line if line.endswith("\n") else line + "\n")
                    continue
                if isinstance(entry, dict) and not keep(entry):
                    removed += 1
                    continue
                handle.write(line if line.endswith("\n") else line + "\n")
        os.replace(replacement, path)
    except BaseException:
        replacement.unlink(missing_ok=True)
        raise
    return removed
