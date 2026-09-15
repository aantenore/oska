"""A preference about how an answer reads, and the line it may never cross.

A judgment of `down` sometimes carries a comment, and a comment is sometimes
not about the answer at all but about the shape of it: too long, no lists,
greet me first. That is worth keeping, and it is worth keeping on a very short
leash.

The leash is one sentence, and it is the whole of this module. A preference may
change how an answer reads. It may never change what the runtime does. It
reaches the reply writer as a fact and reaches nothing else: not the planner,
not a policy, not a payload, not a routing decision. A comment that asks for
something to be done is not a preference about reading and is discarded rather
than stored, because a stored one would be a standing instruction nobody
approved.

Classification happens at job time and never during a turn. A turn that had to
decide what a comment meant would be paying a model call for something nobody
asked it for, and would be acting on a comment before anyone had read it.

A preference is a file under the state directory, like a learned phrasing.
Deleting the file is the whole of the undo, and `improve revert` deletes it.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from opsdeck.config import Settings
from opsdeck.contracts.common import Message
from opsdeck.improvement.feedback import TurnFeedback
from opsdeck.planner.provider import ModelProvider, ModelProviderError

__all__ = [
    "CLASSIFY_SCHEMA",
    "PREFERENCES_DIRNAME",
    "Preference",
    "PreferenceStore",
    "classify_comment",
    "default_preferences_root",
    "learn_preferences",
    "preferences_path",
    "preferences_root",
    "read_preferences",
    "revert_preferences",
    "write_preferences",
]

PREFERENCES_DIRNAME = "preferences"
PREFERENCES_SUFFIX = ".json"
MAX_PREFERENCES_PER_SESSION = 8
PRESENTATION = "presentation"

CLASSIFY_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["kind", "preference"],
    "properties": {
        "kind": {
            "type": "string",
            "enum": [PRESENTATION, "action"],
            "description": (
                "'presentation' when the comment asks for a different way of writing the"
                " answer, such as its length, its tone, its structure or its greeting."
                " 'action' when it asks for anything to be done, decided, remembered,"
                " looked up or changed."
            ),
        },
        "preference": {
            "type": "string",
            "description": (
                "The presentation preference as one short instruction about how to write,"
                " or an empty string when the kind is 'action'."
            ),
        },
    },
}

CLASSIFY_SYSTEM = (
    "You decide what one comment about an answer is asking for.\n\n"
    "Rules:\n"
    "- A comment is a presentation preference only when it is about how the answer"
    " is written: its length, its tone, its structure, its ordering, its greeting.\n"
    "- A comment that asks for something to be done, decided, looked up, remembered"
    " or changed is an action, whatever else it also says.\n"
    "- When you are not sure, answer 'action'.\n"
    "- Rewrite a presentation preference as one short instruction about how to write,"
    " naming no system, no operation and no identifier."
)


class Preference(BaseModel):
    """One thing a person asked for about how answers read."""

    text: str
    session_id: str = ""
    turn: int = 0
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))


def preferences_root(state_dir: Path) -> Path:
    """Where preferences are kept, under the state directory and nowhere else."""
    return Path(state_dir) / PREFERENCES_DIRNAME


def default_preferences_root() -> Path:
    """The preference directory of the installation named by the environment.

    The reply writer is built with the reply configuration and nothing else, so
    a caller that names no directory gets the one the settings of this
    installation point at.
    """
    return preferences_root(Path(Settings.from_env().state_dir))


def preferences_path(root: Path, session_id: str) -> Path:
    """The file holding the preferences of one session."""
    name = Path(session_id).name
    if not name or name != session_id:
        raise KeyError(f"unusable session id: {session_id!r}")
    return Path(root) / f"{name}{PREFERENCES_SUFFIX}"


def read_preferences(root: Path, session_id: str) -> list[Preference]:
    """The preferences held for one session, or nothing when there are none.

    Nothing here fails a turn. A directory that is not there, a file that
    cannot be read and a document that is not a list of preferences all mean
    the same thing: this session has no preference on record.
    """
    try:
        path = preferences_path(root, session_id)
    except KeyError:
        return []
    if not path.is_file():
        return []
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        return [
            Preference.model_validate(entry) for entry in document.get("preferences", [])
        ]
    except (OSError, ValueError, AttributeError, json.JSONDecodeError):
        return []


def write_preferences(
    root: Path,
    session_id: str,
    preferences: Sequence[Preference],
    *,
    at: datetime | None = None,
) -> Path:
    """Write the preferences of one session, replacing any earlier file."""
    path = preferences_path(root, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "recorded_at": (at or datetime.now(UTC)).isoformat(),
        "preferences": [
            preference.model_dump(mode="json")
            for preference in preferences[:MAX_PREFERENCES_PER_SESSION]
        ],
    }
    path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def revert_preferences(root: Path, session_id: str) -> bool:
    """Delete the preferences of one session. True when there was a file to delete."""
    try:
        path = preferences_path(root, session_id)
    except KeyError:
        return False
    if not path.is_file():
        return False
    path.unlink()
    return True


class PreferenceStore:
    """The preference files of every session, as removal sees them.

    The functions above are the whole of reading and writing a preference. This
    is the one thing a caller that removes the record of a conversation needs:
    a session named, and a count of what went.
    """

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        """The directory this store reads and removes under."""
        return self._root

    def delete(self, session_id: str) -> int:
        """Remove the preferences of one session: 1 when there was a file, 0 otherwise."""
        return int(revert_preferences(self._root, session_id))


def classify_comment(comment: str, provider: ModelProvider) -> str | None:
    """The presentation preference a comment holds, or nothing at all.

    Nothing is the answer for a comment that asks for something to be done, for
    a comment the model could not classify, and for a provider that failed. The
    quiet answer is the safe one here: a discarded comment costs a person a
    repetition, and a stored one that asked for an action would be a standing
    instruction to act.
    """
    text = comment.strip()
    if not text:
        return None
    try:
        answer = provider.complete_json(
            CLASSIFY_SYSTEM,
            [Message(role="user", content=text)],
            CLASSIFY_SCHEMA,
        )
    except ModelProviderError:
        return None
    if answer.get("kind") != PRESENTATION:
        return None
    preference = answer.get("preference")
    if not isinstance(preference, str) or not preference.strip():
        return None
    return preference.strip()


def learn_preferences(
    judgments: Sequence[TurnFeedback],
    root: Path,
    *,
    provider: ModelProvider | None = None,
) -> dict[str, list[Preference]]:
    """Classify the comments of the judgments that carry one, and record what fits.

    Only a `down` carrying a comment is looked at: a judgment with no comment
    says nothing about how the answer read, and an `up` is not a complaint about
    the shape of it. Without a provider nothing is classified and nothing is
    recorded, because the alternative is guessing at what a sentence meant.

    What a session already holds is kept, so a run of the job adds to the record
    of a session rather than replacing it. The answer is what each touched
    session holds afterwards.
    """
    if provider is None:
        return {}
    recorded: dict[str, list[Preference]] = {}
    for judgment in judgments:
        if judgment.score != "down" or not judgment.comment.strip():
            continue
        text = classify_comment(judgment.comment, provider)
        if text is None:
            continue
        held = recorded.setdefault(
            judgment.session_id, read_preferences(root, judgment.session_id)
        )
        if any(preference.text == text for preference in held):
            continue
        held.append(
            Preference(
                text=text,
                session_id=judgment.session_id,
                turn=judgment.turn,
                at=judgment.at,
            )
        )
    for session_id, preferences in recorded.items():
        write_preferences(root, session_id, preferences)
    return recorded
