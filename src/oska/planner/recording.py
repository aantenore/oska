"""Capturing a live session to a cassette, and replaying it offline.

A cassette is one session written down as it happened: every completion with the
request that produced it and the answer that came back, and every embedding call
with the texts and the vectors. Recording wraps whichever provider is in force
and writes the file as it goes, so a run that dies half way keeps what it got.
Replaying spends the same file in order and needs no endpoint and no key, which
is how a real exchange becomes a test that runs offline.

A cassette is close to a scenario `provider_script` but not the same thing: it
also holds the requests and the embeddings. `cassette_to_script` drops what a
scenario does not read, so a recorded session seeds a scenario directly.

A recorded expectation must not hold a value a run makes up for itself. A
receipt id, an approval id, a proposal id, a turn id — every one of them is an
opaque identifier drawn fresh each run (`uuid.uuid4()`), so an expectation that
keeps one only ever matches the run that recorded it. `DEFAULT_VOLATILE`
patterns such an identifier the same way everywhere an expectation is derived
or read back: a match is where the expectation is cut, and an expectation left
with nothing stable is dropped rather than kept as an empty string. `volatile`
lets a caller replace those patterns; it never adds to them, so a deployment
whose own identifiers do not look like a uuid names its own instead.

`KeyedReplayProvider` is the served form of a replay. Over HTTP many sessions
share one provider and their calls interleave, so an answer cannot be the next
one in the file: it is the recorded answer to this request. A request is found
by what it asks, the purpose of the call and the last user message cut before
any volatile value, and among the steps that ask the same, by the conversation
that led to it and then by which of them this session has not used yet.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from oska.contracts.common import Message
from oska.planner.provider import ModelProvider, ModelProviderError, provider_embeds
from oska.planner.scripted import (
    EMBEDDING_DIMENSIONS,
    ScriptedModelProvider,
    ScriptMismatchError,
    hash_bag_vector,
)

__all__ = [
    "DEFAULT_VOLATILE",
    "EXHAUSTED_HINT",
    "EXPECT_LENGTH",
    "PURPOSE_WORDS",
    "UNKNOWN_REQUEST",
    "KeyedReplayProvider",
    "RecordingProvider",
    "ReplayExhaustedError",
    "ReplayProvider",
    "cassette_digest",
    "cassette_to_script",
    "read_cassette",
    "replay_session",
]

EXPECT_LENGTH = 60
EXHAUSTED_HINT = (
    "the session may have been replayed already; --replay-from-start, "
    "or a new state directory, starts over"
)
PURPOSE_WORDS = 8
UNKNOWN_REQUEST = (
    "this demo knows only the conversation recorded in it, and the request is not part of it"
)
SESSION_LIMIT = 1024
"""How many sessions a served replay remembers the used steps of; the oldest go first."""

_SESSION: ContextVar[str | None] = ContextVar("oska_replay_session", default=None)

DEFAULT_VOLATILE: tuple[str, ...] = (
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    r"\b[0-9a-f]{32}\b",
)
"""The canonical uuid form, and a bare 32-hex uuid with its dashes stripped."""


class RecordingProvider:
    """Delegates to another provider and keeps every call.

    The file is rewritten after each call, so it is valid JSON at any moment and
    a crashed run leaves the steps it already had. A cassette that exists is
    read first and added to, which is how the turns of one session end up in one
    file across several commands.
    """

    def __init__(
        self, inner: ModelProvider, path: Path, *, volatile: Sequence[str] | None = None
    ) -> None:
        self._inner = inner
        self._path = Path(path)
        self._volatile = _compile_volatile(volatile)
        self._steps: list[dict] = read_cassette(self._path) if self._path.exists() else []
        self.name = f"recording:{getattr(inner, 'name', 'provider')}"

    @property
    def embeds(self) -> bool:
        """Whether the wrapped provider embeds, so the runtime ranks as it would without us."""
        return provider_embeds(self._inner)

    @property
    def path(self) -> Path:
        """Where the cassette is written."""
        return self._path

    def complete_json(
        self,
        system: str,
        messages: list[Message],
        schema: dict,
        *,
        temperature: float = 0.0,
    ) -> dict:
        """Ask the wrapped provider and record the exchange."""
        response = self._inner.complete_json(system, messages, schema, temperature=temperature)
        step: dict = {"purpose": _purpose(system, schema)}
        expectation = _expect_contains(messages, self._volatile)
        if expectation:
            step["expect_contains"] = expectation
        step["request"] = {
            "system": system,
            "messages": [message.model_dump(mode="json") for message in messages],
            "schema": schema,
        }
        step["response"] = response
        self._append(step)
        return response

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Ask the wrapped provider and record the vectors it returned."""
        vectors = self._inner.embed(texts)
        self._append({"embed": {"texts": list(texts), "vectors": [list(v) for v in vectors]}})
        return vectors

    def _append(self, step: dict) -> None:
        self._steps.append(step)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("w", encoding="utf-8") as handle:
            json.dump(self._steps, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()


class ReplayExhaustedError(ScriptMismatchError):
    """A positional replay was asked for one completion more than the cassette holds.

    A `ScriptMismatchError` and not a `ModelProviderError`: the runtime answers
    around a model that fails, and answering around a cassette that ran out
    would hide that the session has drifted from the recording, or was replayed
    already. It stops the turn, and the message says how to start over.
    """


class ReplayProvider:
    """Answers from a cassette, in the order the cassette was written.

    Completions are spent one per call and checked against `expect_contains`,
    exactly as a scenario script is. Embeddings are looked up by text; a text the
    cassette never saw falls back to the deterministic hash bag, so a catalog
    that grew since the recording still ranks instead of failing.

    `start` is how many recorded completions are already spent, so a session
    replayed one turn per process can pick up where the last process stopped.
    `position` is where this provider stands in the cassette; the caller keeps it
    between processes, the provider reads nothing but the cassette.
    """

    name = "replay"

    def __init__(
        self, path: Path, *, volatile: Sequence[str] | None = None, start: int = 0
    ) -> None:
        if start < 0:
            raise ValueError("start is a count of completions and cannot be negative")
        self._path = Path(path)
        self._start = start
        patterns = _compile_volatile(volatile)
        steps = _cut_stored_expectations(read_cassette(self._path), patterns)
        self._completions = ScriptedModelProvider(_completion_steps(steps)[start:])
        self._vectors = _recorded_vectors(steps)
        self._dimensions = _dimensions(self._vectors)
        self.embeds = any("embed" in step for step in steps)

    @property
    def path(self) -> Path:
        """Which cassette is being replayed."""
        return self._path

    def remaining(self) -> int:
        """How many recorded completions have not been spent."""
        return self._completions.remaining()

    @property
    def position(self) -> int:
        """How many recorded completions have been spent, counting those skipped by `start`."""
        return self._start + self._completions.consumed()

    def complete_json(
        self,
        system: str,
        messages: list[Message],
        schema: dict,
        *,
        temperature: float = 0.0,
    ) -> dict:
        """Return the next recorded answer."""
        exhausted = self.remaining() == 0
        try:
            return self._completions.complete_json(system, messages, schema)
        except ScriptMismatchError as exc:
            raise ScriptMismatchError(f"cassette {self._path}: {exc}") from exc
        except ModelProviderError as exc:
            if exhausted:
                raise ReplayExhaustedError(
                    f"cassette {self._path}: {exc} ({EXHAUSTED_HINT})"
                ) from exc
            raise ModelProviderError(f"cassette {self._path}: {exc}") from exc

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return the recorded vector of each text, or a deterministic one."""
        return [
            self._vectors.get(text) or hash_bag_vector(text, self._dimensions) for text in texts
        ]


@contextmanager
def replay_session(session_id: str | None) -> Iterator[None]:
    """Name the session the calls made inside this block belong to, for a served replay."""
    token = _SESSION.set(session_id)
    try:
        yield
    finally:
        _SESSION.reset(token)


class KeyedReplayProvider:
    """Answers from a cassette by the request, not by position, for many sessions at once.

    A completion is answered by the recorded step that asked the same thing: the
    same purpose and the same last user message, each cut before any volatile
    value. When several steps ask the same, the one whose conversation agrees
    longest with this request's wins; then the first one the calling session
    has not used yet; then the first in the file. A request no step asked raises
    `ModelProviderError`, which the runtime answers around as it would answer
    around a model that failed.

    A recorded answer may repeat an identifier its request carried, a receipt id
    above all, and that identifier was drawn for the recorded run. When the
    recorded request and this one carry the same number of distinct volatile
    values, each recorded value in the answer is replaced by the value at the
    same place in this request, so the answer names this run's receipt.

    The session is the one `replay_session` names, so the HTTP application sets
    it per request; calls outside any session share one record of used steps.
    The record is held under a lock, so overlapping turns on several threads
    each take their own answer. Embeddings are looked up by text, as in
    `ReplayProvider`.
    """

    name = "replay"

    def __init__(self, path: Path, *, volatile: Sequence[str] | None = None) -> None:
        self._path = Path(path)
        self._patterns = _compile_volatile(volatile)
        steps = read_cassette(self._path)
        self._steps = [
            _KeyedStep(index, step, self._patterns)
            for index, step in enumerate(_completion_steps(steps))
        ]
        self._vectors = _recorded_vectors(steps)
        self._dimensions = _dimensions(self._vectors)
        self.embeds = any("embed" in step for step in steps)
        self._lock = threading.Lock()
        self._used: dict[str | None, set[int]] = {}

    @property
    def path(self) -> Path:
        """Which cassette is being replayed."""
        return self._path

    def complete_json(
        self,
        system: str,
        messages: list[Message],
        schema: dict,
        *,
        temperature: float = 0.0,
    ) -> dict:
        """Return the recorded answer to this request."""
        purpose = _purpose(system, schema)
        asked = _last_user(messages, self._patterns)
        history = _history([(m.role, m.content) for m in messages], self._patterns)
        candidates = [step for step in self._steps if step.answers(purpose, asked)]
        live = [message.content for message in messages]
        if not candidates:
            raise ModelProviderError(f"cassette {self._path}: {UNKNOWN_REQUEST}")
        session = _SESSION.get()
        with self._lock:
            used = self._used.pop(session, set())
            self._used[session] = used
            while len(self._used) > SESSION_LIMIT:
                del self._used[next(iter(self._used))]
            chosen = max(
                candidates,
                key=lambda step: (*step.agreement(history), step.index not in used, -step.index),
            )
            used.add(chosen.index)
        if not isinstance(chosen.response, dict):
            raise ModelProviderError(
                f"cassette {self._path}: completion {chosen.index + 1} holds no answer object"
            )
        return _renamed(chosen.response, chosen.carried, live, self._patterns)

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return the recorded vector of each text, or a deterministic one."""
        return [
            self._vectors.get(text) or hash_bag_vector(text, self._dimensions) for text in texts
        ]


class _KeyedStep:
    """One recorded completion, with what it asked worked out once."""

    __slots__ = ("asked", "carried", "expected", "history", "index", "purpose", "response")

    def __init__(self, index: int, step: dict, patterns: Sequence[re.Pattern[str]]) -> None:
        self.index = index
        self.response = step.get("response", {})
        request = step.get("request")
        raw = request.get("messages") if isinstance(request, dict) else None
        self.carried: list[str] = []
        if isinstance(request, dict) and isinstance(raw, list):
            self.carried = [
                str(message.get("content", "")) for message in raw if isinstance(message, dict)
            ]
            self.purpose = _purpose(str(request.get("system", "")), request.get("schema") or {})
            pairs = [
                (str(message.get("role", "")), str(message.get("content", "")))
                for message in raw
                if isinstance(message, dict)
            ]
            self.history: tuple[tuple[str, str], ...] | None = _history(pairs, patterns)
            self.asked: str | None = next(
                (content for role, content in reversed(self.history) if role == "user"), ""
            )
            self.expected = ""
        else:
            self.purpose = str(step.get("purpose", ""))
            self.history = None
            self.asked = None
            self.expected = _cut_stored_expectation(step.get("expect_contains"), patterns)

    def answers(self, purpose: str, asked: str) -> bool:
        """Whether this step asked what the live request asks."""
        if purpose != self.purpose:
            return False
        if self.asked is not None:
            return asked == self.asked
        return self.expected in asked

    def agreement(self, history: tuple[tuple[str, str], ...]) -> tuple[int, int]:
        """How far the recorded conversation agrees with this one, and how close its length is."""
        if self.history is None:
            return (0, 0)
        shared = 0
        for recorded, live in zip(self.history, history, strict=False):
            if recorded != live:
                break
            shared += 1
        return (shared, -abs(len(self.history) - len(history)))


def _renamed(
    response: dict,
    recorded: Sequence[str],
    live: Sequence[str],
    patterns: Sequence[re.Pattern[str]],
) -> dict:
    """A copy of `response`, its recorded volatile values replaced by this request's.

    The values are paired by the order they first appear in each request. When
    the two requests do not carry the same number of them, the pairing would be
    a guess, and the answer is returned as it was recorded.
    """
    text = json.dumps(response, ensure_ascii=False)
    before = _volatile_values(recorded, patterns)
    after = _volatile_values(live, patterns)
    if before and len(before) == len(after):
        renames = dict(zip(before, after, strict=True))
        for pattern in patterns:
            text = pattern.sub(lambda match: renames.get(match.group(0), match.group(0)), text)
    renamed: dict = json.loads(text)
    return renamed


def _volatile_values(texts: Sequence[str], patterns: Sequence[re.Pattern[str]]) -> list[str]:
    """Every distinct volatile value in `texts`, in the order each first appears."""
    found: dict[str, None] = {}
    for text in texts:
        matches = sorted(
            (match.start(), match.group(0))
            for pattern in patterns
            for match in pattern.finditer(text)
        )
        for _, value in matches:
            found.setdefault(value, None)
    return list(found)


def _history(
    pairs: Sequence[tuple[str, str]], patterns: Sequence[re.Pattern[str]]
) -> tuple[tuple[str, str], ...]:
    """A conversation as (role, content) pairs, each content cut before any volatile value."""
    return tuple((role, _cut_volatile(content.strip(), patterns)) for role, content in pairs)


def _last_user(messages: list[Message], patterns: Sequence[re.Pattern[str]]) -> str:
    """The last user message, cut before any volatile value; empty when there is none."""
    for message in reversed(messages):
        if message.role == "user":
            return _cut_volatile(message.content.strip(), patterns)
    return ""


def cassette_digest(path: Path) -> str:
    """The SHA-256 of the cassette's bytes, which says whether its content changed."""
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError as exc:
        raise ModelProviderError(f"the cassette could not be read: {exc}") from exc


def read_cassette(path: Path) -> list[dict]:
    """Read a cassette file and return its steps."""
    try:
        with Path(path).open(encoding="utf-8") as handle:
            data = json.load(handle)
    except OSError as exc:
        raise ModelProviderError(f"the cassette could not be read: {exc}") from exc
    except ValueError as exc:
        raise ModelProviderError(f"{path}: the cassette is not valid JSON: {exc}") from exc
    if isinstance(data, dict):
        data = data.get("steps", data.get("provider_script"))
    if not isinstance(data, list):
        raise ModelProviderError(f"{path}: a cassette is a list of steps")
    for position, step in enumerate(data, start=1):
        if not isinstance(step, dict):
            raise ModelProviderError(f"{path}: cassette step {position} is not an object")
    return list(data)


def cassette_to_script(path: Path, *, volatile: Sequence[str] | None = None) -> list[dict]:
    """The completions of a cassette as a scenario `provider_script`.

    The requests and the embeddings are dropped; what is left is what a scenario
    reads: the label, the assertion about the prompt, and the answer.
    """
    patterns = _compile_volatile(volatile)
    script: list[dict] = []
    for step in _completion_steps(read_cassette(path)):
        entry: dict = {"purpose": step.get("purpose", ""), "response": step.get("response", {})}
        expected = _recut_expectation(step, patterns) or ""
        if expected:
            entry["expect_contains"] = expected
        script.append(entry)
    return script


def _completion_steps(steps: list[dict]) -> list[dict]:
    return [step for step in steps if "embed" not in step]


def _recorded_vectors(steps: list[dict]) -> dict[str, list[float]]:
    recorded: dict[str, list[float]] = {}
    for step in steps:
        block = step.get("embed")
        if not isinstance(block, dict):
            continue
        texts = block.get("texts") or []
        vectors = block.get("vectors") or []
        for text, vector in zip(texts, vectors, strict=False):
            if isinstance(text, str) and isinstance(vector, list):
                recorded[text] = [float(value) for value in vector]
    return recorded


def _dimensions(vectors: dict[str, list[float]]) -> int:
    """The width the fallback must produce, so every vector compares."""
    for vector in vectors.values():
        if vector:
            return len(vector)
    return EMBEDDING_DIMENSIONS


def _purpose(system: str, schema: dict) -> str:
    """A label for the call: the title of the schema, else how the prompt opens."""
    title = schema.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    first_line = next((line for line in system.splitlines() if line.strip()), "")
    words = first_line.split()[:PURPOSE_WORDS]
    return " ".join(words).rstrip(".,:") or "completion"


def _expect_contains(messages: list[Message], patterns: Sequence[re.Pattern[str]]) -> str:
    """The opening of the last user message, cut before any volatile value.

    The cut is applied to the whole message, not to its first sixty characters:
    a volatile value that starts within that window would otherwise survive it
    in truncated, still-unstable form. What is left is then truncated to
    `EXPECT_LENGTH`, which is what the replay asserts on.
    """
    for message in reversed(messages):
        if message.role == "user":
            return _stable_prefix(message.content, patterns)
    return ""


def _stable_prefix(content: str, patterns: Sequence[re.Pattern[str]]) -> str:
    """`content`, cut before any volatile value and truncated to `EXPECT_LENGTH`."""
    stable = _cut_volatile(content.strip(), patterns)
    return stable[:EXPECT_LENGTH] if stable.strip() else ""


def _cut_stored_expectations(steps: list[dict], patterns: Sequence[re.Pattern[str]]) -> list[dict]:
    """Steps with `expect_contains` cut again, for a cassette recorded before this cut existed."""
    cut: list[dict] = []
    for step in steps:
        expected = _recut_expectation(step, patterns)
        if expected is None or expected == step.get("expect_contains", ""):
            cut.append(step)
            continue
        replaced = dict(step)
        if expected:
            replaced["expect_contains"] = expected
        else:
            replaced.pop("expect_contains", None)
        cut.append(replaced)
    return cut


def _recut_expectation(step: dict, patterns: Sequence[re.Pattern[str]]) -> str | None:
    """The expectation of a step, cut as a fresh recording would cut it.

    A cassette keeps the full request beside the cut opening of its last user
    message, so recomputing from `request.messages` recovers exactly what
    `_expect_contains` would write today — even where the stored value was
    truncated to sixty characters mid volatile value, before this cut existed,
    and a match against the truncated fragment alone would miss it. `None` is
    returned only when the step holds no usable request to recompute from (an
    embedding step, or one already reduced to `purpose`/`expect_contains`/
    `response` by `cassette_to_script`); there, cutting whatever string is
    already stored is all that is left to do.
    """
    from_request = _expect_contains_from_request(step, patterns)
    if from_request is not None:
        return from_request
    if "expect_contains" not in step:
        return None
    return _cut_stored_expectation(step.get("expect_contains"), patterns)


def _expect_contains_from_request(step: dict, patterns: Sequence[re.Pattern[str]]) -> str | None:
    """`_expect_contains`, applied to the raw request messages a cassette step carries."""
    request = step.get("request")
    if not isinstance(request, dict):
        return None
    raw_messages = request.get("messages")
    if not isinstance(raw_messages, list):
        return None
    for message in reversed(raw_messages):
        if isinstance(message, dict) and message.get("role") == "user":
            content = message.get("content")
            return _stable_prefix(content, patterns) if isinstance(content, str) else None
    return ""


def _cut_stored_expectation(expected: object, patterns: Sequence[re.Pattern[str]]) -> str:
    """A stored expectation, cut before any volatile value; empty when nothing stable is left."""
    if not isinstance(expected, str):
        return ""
    stable = _cut_volatile(expected, patterns)
    return stable if stable.strip() else ""


def _cut_volatile(text: str, patterns: Sequence[re.Pattern[str]]) -> str:
    """The text up to the earliest match of any pattern, or the text unchanged."""
    cut = len(text)
    for pattern in patterns:
        match = pattern.search(text)
        if match is not None:
            cut = min(cut, match.start())
    return text[:cut]


def _compile_volatile(patterns: Sequence[str] | None) -> list[re.Pattern[str]]:
    """The volatile patterns, compiled; the defaults unless a caller names its own."""
    source = DEFAULT_VOLATILE if patterns is None else patterns
    compiled: list[re.Pattern[str]] = []
    for pattern in source:
        try:
            compiled.append(re.compile(pattern, re.IGNORECASE))
        except re.error as exc:
            raise ModelProviderError(
                f"{pattern!r} is not a valid regular expression: {exc}"
            ) from exc
    return compiled
