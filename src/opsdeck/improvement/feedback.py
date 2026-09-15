"""A judgment on an answer, recorded against the turn that gave it.

Level 0 reads what a turn left behind: what was asked, where it was routed,
what ran, and whether a write was confirmed. None of that says whether the
answer was any good. A person knows, and until they are asked the system never
finds out.

So this is the smallest honest addition: a score of `up` or `down` against one
turn of one session, with an optional comment. It is one call. It writes one
line to a file under the state directory and does nothing else — no procedure
changes, no threshold moves, no wording is learned because of it. What reads it
decides what it is worth, and the scheduled job is the only thing that reads it
for anything beyond reporting.

A turn is named by its position in the session, because that is the number a
person can see; the identifier the audit trail gives a turn is a random one
nobody could quote back.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

__all__ = [
    "FEEDBACK_DIRNAME",
    "FeedbackStore",
    "Score",
    "TurnFeedback",
    "feedback_path",
    "feedback_root",
    "latest",
    "scores",
]

FEEDBACK_DIRNAME = "feedback"
FEEDBACK_SUFFIX = ".jsonl"

Score = Literal["up", "down"]
SCORES: tuple[str, ...] = ("up", "down")


class TurnFeedback(BaseModel):
    """What one person said about one answer."""

    session_id: str
    turn: int
    score: Score
    comment: str = ""
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))


def feedback_root(state_dir: Path) -> Path:
    """Where judgments are kept, under the state directory and nowhere else."""
    return Path(state_dir) / FEEDBACK_DIRNAME


def feedback_path(root: Path, session_id: str) -> Path:
    """The file holding the judgments of one session."""
    name = Path(session_id).name
    if not name or name != session_id:
        raise KeyError(f"unusable session id: {session_id!r}")
    return Path(root) / f"{name}{FEEDBACK_SUFFIX}"


class FeedbackStore:
    """The judgments of every session, one append-only file each.

    Appending is the whole of the write. A turn judged twice keeps both lines,
    and what reads them takes the last one: a person who changes their mind is
    not an error to be prevented.
    """

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        """The directory this store writes under."""
        return self._root

    def record(
        self,
        session_id: str,
        turn: int,
        score: Score,
        comment: str = "",
        *,
        at: datetime | None = None,
    ) -> TurnFeedback:
        """Record one judgment and return it. Nothing else changes."""
        if score not in SCORES:
            raise ValueError(f"a score is {' or '.join(SCORES)}, not {score!r}")
        if turn < 1:
            raise ValueError(f"a turn is counted from 1, not {turn!r}")
        judgment = TurnFeedback(
            session_id=session_id,
            turn=turn,
            score=score,
            comment=comment.strip(),
            at=at or datetime.now(UTC),
        )
        path = feedback_path(self._root, session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(judgment.model_dump_json() + "\n")
        return judgment

    def delete(self, session_id: str) -> int:
        """Remove the judgments of one session and report how many went.

        A judgment names a session and a turn of it, so it is part of the
        record of that conversation and goes when the conversation does. This
        is what retention calls; nothing in a turn calls it.
        """
        try:
            path = feedback_path(self._root, session_id)
        except KeyError:
            return 0
        if not path.is_file():
            return 0
        removed = len(_read(path))
        path.unlink()
        return removed

    def for_session(self, session_id: str) -> list[TurnFeedback]:
        """Every judgment recorded for one session, in the order it was recorded."""
        try:
            path = feedback_path(self._root, session_id)
        except KeyError:
            return []
        return _read(path)

    def all(self) -> list[TurnFeedback]:
        """Every judgment this store holds, oldest first."""
        if not self._root.is_dir():
            return []
        judgments: list[TurnFeedback] = []
        for path in sorted(self._root.glob(f"*{FEEDBACK_SUFFIX}")):
            judgments.extend(_read(path))
        judgments.sort(key=lambda judgment: judgment.at)
        return judgments


def latest(judgments: Iterable[TurnFeedback]) -> dict[tuple[str, int], TurnFeedback]:
    """The last judgment recorded for each turn, by session and position."""
    found: dict[tuple[str, int], TurnFeedback] = {}
    for judgment in judgments:
        found[(judgment.session_id, judgment.turn)] = judgment
    return found


def _read(path: Path) -> list[TurnFeedback]:
    """The judgments of one file. An unreadable line is skipped, not fatal."""
    if not path.is_file():
        return []
    judgments: list[TurnFeedback] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                judgments.append(TurnFeedback.model_validate_json(stripped))
            except (ValueError, json.JSONDecodeError):
                continue
    return judgments


def scores(judgments: Sequence[TurnFeedback]) -> dict[str, int]:
    """How many of each score these judgments hold."""
    counted = {score: 0 for score in SCORES}
    for judgment in judgments:
        counted[judgment.score] = counted.get(judgment.score, 0) + 1
    return counted
