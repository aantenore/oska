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
"""

from __future__ import annotations

import json
from pathlib import Path

from oska.contracts.common import Message
from oska.planner.provider import ModelProvider, ModelProviderError
from oska.planner.scripted import (
    EMBEDDING_DIMENSIONS,
    ScriptedModelProvider,
    hash_bag_vector,
)

__all__ = [
    "EXPECT_LENGTH",
    "PURPOSE_WORDS",
    "RecordingProvider",
    "ReplayProvider",
    "cassette_to_script",
    "read_cassette",
]

EXPECT_LENGTH = 60
PURPOSE_WORDS = 8


class RecordingProvider:
    """Delegates to another provider and keeps every call.

    The file is rewritten after each call, so it is valid JSON at any moment and
    a crashed run leaves the steps it already had. A cassette that exists is
    read first and added to, which is how the turns of one session end up in one
    file across several commands.
    """

    def __init__(self, inner: ModelProvider, path: Path) -> None:
        self._inner = inner
        self._path = Path(path)
        self._steps: list[dict] = read_cassette(self._path) if self._path.exists() else []
        self.name = f"recording:{getattr(inner, 'name', 'provider')}"

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
        self._append(
            {
                "purpose": _purpose(system, schema),
                "expect_contains": _expect_contains(messages),
                "request": {
                    "system": system,
                    "messages": [message.model_dump(mode="json") for message in messages],
                    "schema": schema,
                },
                "response": response,
            }
        )
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


class ReplayProvider:
    """Answers from a cassette, in the order the cassette was written.

    Completions are spent one per call and checked against `expect_contains`,
    exactly as a scenario script is. Embeddings are looked up by text; a text the
    cassette never saw falls back to the deterministic hash bag, so a catalog
    that grew since the recording still ranks instead of failing.
    """

    name = "replay"

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        steps = read_cassette(self._path)
        self._completions = ScriptedModelProvider(_completion_steps(steps))
        self._vectors = _recorded_vectors(steps)
        self._dimensions = _dimensions(self._vectors)

    @property
    def path(self) -> Path:
        """Which cassette is being replayed."""
        return self._path

    def remaining(self) -> int:
        """How many recorded completions have not been spent."""
        return self._completions.remaining()

    def complete_json(
        self,
        system: str,
        messages: list[Message],
        schema: dict,
        *,
        temperature: float = 0.0,
    ) -> dict:
        """Return the next recorded answer."""
        try:
            return self._completions.complete_json(system, messages, schema)
        except ModelProviderError as exc:
            raise ModelProviderError(f"cassette {self._path}: {exc}") from exc

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return the recorded vector of each text, or a deterministic one."""
        return [
            self._vectors.get(text) or hash_bag_vector(text, self._dimensions) for text in texts
        ]


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


def cassette_to_script(path: Path) -> list[dict]:
    """The completions of a cassette as a scenario `provider_script`.

    The requests and the embeddings are dropped; what is left is what a scenario
    reads: the label, the assertion about the prompt, and the answer.
    """
    script: list[dict] = []
    for step in _completion_steps(read_cassette(path)):
        entry: dict = {"purpose": step.get("purpose", ""), "response": step.get("response", {})}
        expected = step.get("expect_contains")
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


def _expect_contains(messages: list[Message]) -> str:
    """The opening of the last user message, which the replay asserts on."""
    for message in reversed(messages):
        if message.role == "user":
            return message.content.strip()[:EXPECT_LENGTH]
    return ""
