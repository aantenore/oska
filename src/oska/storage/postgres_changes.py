"""Change requests kept in PostgreSQL, packages included, so every replica reviews one queue.

A request is one row holding the whole record as JSON, and its package is one
row per file, named by its path inside the package. Nothing of a request lives
on the disk of the replica that composed it, so any replica can show it,
publish it as a branch, or have a person apply it into a catalog root.

A decision is taken under a row lock: two replicas asked to apply or reject the
same request at once find it open one at a time, and the second is refused
because the first has decided. Every row and every query carries the namespace
of the store; `purge` removes the whole of it and refuses the default.
"""

from __future__ import annotations

import builtins
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from oska.contracts.change import (
    POLICY_FILENAME,
    SKILL_FILENAME,
    ChangeRequest,
    ChangeRequestError,
    ChangeRequestState,
    package_file,
    require_actor,
    require_open,
)
from oska.storage.postgres import Database, jsonb
from oska.storage.protocols import DEFAULT_NAMESPACE, refuse_default_purge

__all__ = ["PostgresChangeRequestStore"]


class PostgresChangeRequestStore:
    """Reads and writes change requests and their packages in PostgreSQL, under one namespace."""

    def __init__(
        self,
        url: str,
        *,
        busy_timeout: float | None = None,
        database: Database | None = None,
        namespace: str = DEFAULT_NAMESPACE,
    ) -> None:
        self._database = database or Database(url, busy_timeout=busy_timeout)
        self._namespace = namespace

    @property
    def namespace(self) -> str:
        """The namespace every request of this store is kept under."""
        return self._namespace

    @property
    def location(self) -> str:
        """The database and the namespace, without the address that may carry a credential."""
        return f"postgres {self._database.identity} namespace {self._namespace}"

    def save(
        self,
        request: ChangeRequest,
        *,
        skill_markdown: str | None = None,
        policy: dict | None = None,
    ) -> str:
        """Write a request, and its package when the file is given. Returns where it is kept.

        A decision rewrites the record and leaves the package alone, as the
        directory store does: what a reviewer approved stays the text they read.
        """
        with self._database.connect() as connection:
            self._write(connection, request)
            if skill_markdown is not None:
                self._put(connection, request.request_id, SKILL_FILENAME, skill_markdown)
                if policy is not None:
                    self._put(
                        connection,
                        request.request_id,
                        POLICY_FILENAME,
                        json.dumps(policy, indent=2) + "\n",
                    )
        return self.location

    def get(self, request_id: str) -> ChangeRequest:
        """Read one request. Raises `KeyError` when it is not there."""
        with self._database.connect() as connection:
            return self._read(connection, request_id)

    def list(
        self, state: ChangeRequestState | str | None = None
    ) -> builtins.list[ChangeRequest]:
        """Every request held, oldest first, filtered by state when one is named."""
        wanted = ChangeRequestState(state) if state is not None else None
        with self._database.connect() as connection:
            if wanted is None:
                rows = connection.execute(
                    "SELECT request FROM oska_change_requests WHERE namespace = %s",
                    (self._namespace,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT request FROM oska_change_requests "
                    "WHERE namespace = %s AND state = %s",
                    (self._namespace, wanted.value),
                ).fetchall()
        held = [ChangeRequest.model_validate(row["request"]) for row in rows]
        held.sort(key=lambda request: (request.created_at, request.request_id))
        return held

    def files(self, request_id: str) -> dict[str, str]:
        """The files of one request's package, by their path inside it."""
        with self._database.connect() as connection:
            self._read(connection, request_id)
            return self._files(connection, request_id)

    def add_file(self, request_id: str, name: str, text: str) -> None:
        """Write one more file into a request's package, replacing one of the same name."""
        with self._database.connect() as connection:
            self._read(connection, request_id)
            self._put(connection, request_id, package_file(name), text)

    def write_package(self, request_id: str, target: Path) -> builtins.list[Path]:
        """Write the package of one request into a directory, and name the files written."""
        files = self.files(request_id)
        if not files:
            raise ChangeRequestError(f"change request {request_id} holds no package")
        return write_files(files, Path(target))

    def apply(
        self, request_id: str, catalog_root: Path, *, by: str
    ) -> builtins.list[Path]:
        """Write the package of one open request into a catalog root a person names.

        The request is locked while it is decided, so no other replica decides
        it in between; an existing version is never overwritten.
        """
        actor = require_actor(by, "applying a change request records who applied it")
        with self._database.connect() as connection:
            request = self._read(connection, request_id, lock=True)
            require_open(request)
            files = self._files(connection, request_id)
            if not files:
                raise ChangeRequestError(f"change request {request_id} holds no package")
            target = Path(catalog_root) / request.package
            if target.exists():
                raise ChangeRequestError(
                    f"{target} is already there: a version is never overwritten"
                )
            written = write_files(files, target)
            request.state = ChangeRequestState.applied
            request.decided_at = datetime.now(UTC)
            request.decided_by = actor
            request.applied_to = str(Path(catalog_root))
            self._write(connection, request)
        return sorted(written)

    def reject(self, request_id: str, *, by: str, note: str = "") -> ChangeRequest:
        """Close one open request without copying anything, recording who and why."""
        actor = require_actor(by, "rejecting a change request records who rejected it")
        with self._database.connect() as connection:
            request = self._read(connection, request_id, lock=True)
            require_open(request)
            request.state = ChangeRequestState.rejected
            request.decided_at = datetime.now(UTC)
            request.decided_by = actor
            request.note = note
            self._write(connection, request)
        return request

    def delete(self, request_id: str) -> None:
        """Remove one request and its package. Raises `KeyError` when it is not there."""
        with self._database.connect() as connection:
            connection.execute(
                "DELETE FROM oska_change_request_files WHERE namespace = %s AND request_id = %s",
                (self._namespace, request_id),
            )
            cursor = connection.execute(
                "DELETE FROM oska_change_requests WHERE namespace = %s AND request_id = %s",
                (self._namespace, request_id),
            )
            removed = int(cursor.rowcount)
        if not removed:
            raise KeyError(f"unknown change request: {request_id}")

    def drop_package(self, request_id: str) -> bool:
        """Remove the package of one request, keep its record, and say whether any went."""
        with self._database.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM oska_change_request_files WHERE namespace = %s AND request_id = %s",
                (self._namespace, request_id),
            )
            return int(cursor.rowcount) > 0

    def purge(self) -> int:
        """Remove every request of this namespace, packages included; report the requests."""
        refuse_default_purge(self._namespace)
        with self._database.connect() as connection:
            connection.execute(
                "DELETE FROM oska_change_request_files WHERE namespace = %s",
                (self._namespace,),
            )
            cursor = connection.execute(
                "DELETE FROM oska_change_requests WHERE namespace = %s", (self._namespace,)
            )
            return int(cursor.rowcount)

    def _read(self, connection: Any, request_id: str, *, lock: bool = False) -> ChangeRequest:
        statement = (
            "SELECT request FROM oska_change_requests WHERE namespace = %s AND request_id = %s"
        )
        row = connection.execute(
            statement + (" FOR UPDATE" if lock else ""), (self._namespace, request_id)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown change request: {request_id}")
        return ChangeRequest.model_validate(row["request"])

    def _files(self, connection: Any, request_id: str) -> dict[str, str]:
        rows = connection.execute(
            "SELECT name, content FROM oska_change_request_files "
            "WHERE namespace = %s AND request_id = %s ORDER BY name",
            (self._namespace, request_id),
        ).fetchall()
        return {row["name"]: row["content"] for row in rows}

    def _write(self, connection: Any, request: ChangeRequest) -> None:
        connection.execute(
            "INSERT INTO oska_change_requests "
            "(namespace, request_id, proposal_id, state, created_at, request) "
            "VALUES (%s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (namespace, request_id) DO UPDATE SET "
            "proposal_id = EXCLUDED.proposal_id, state = EXCLUDED.state, "
            "request = EXCLUDED.request",
            (
                self._namespace,
                request.request_id,
                request.proposal_id,
                request.state.value,
                request.created_at,
                jsonb(request.model_dump(mode="json")),
            ),
        )

    def _put(self, connection: Any, request_id: str, name: str, text: str) -> None:
        connection.execute(
            "INSERT INTO oska_change_request_files (namespace, request_id, name, content) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (namespace, request_id, name) DO UPDATE SET content = EXCLUDED.content",
            (self._namespace, request_id, name, text),
        )


def write_files(files: dict[str, str], target: Path) -> builtins.list[Path]:
    """Write a package, given by path inside it, into a directory; name the files written."""
    written = []
    for name, text in sorted(files.items()):
        path = Path(target) / package_file(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        written.append(path)
    return written
