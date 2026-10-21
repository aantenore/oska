"""A connector that calls tools on a server speaking the Model Context Protocol.

This is a client, and a narrow one. Each operation the binding declares names
one tool and the template of its arguments; nothing else on the server can be
reached through it. The server's own list of tools is never handed to a model
and never consulted while a turn runs: an operation exists because a skill
declares it and a binding maps it, not because a server offers it. The list is
read only by `unlisted_tools`, which a pack check uses to say that a declared
operation points at a tool the server does not have.

The protocol is JSON-RPC 2.0 over one of two transports:

- `stdio`: the binding declares an argument vector, and the server is started
  from it for each call, never through a shell. It sees the search path of
  this process and what the binding's `env` declares, nothing else of the
  environment. Messages are single lines of JSON on its input and output.
- `http`: the streamable HTTP transport. Every message is a POST to the
  declared URL, and an answer comes back either as a JSON body or as a stream
  of server-sent events.

A call is an exchange: `initialize`, the `notifications/initialized` notice,
then `tools/call` with the tool's name and its arguments.

The arguments are built from the declared template, one value at a time. A
string that is exactly `{field}` becomes the payload value of that field, of
whatever type it is, and is left out when the payload does not carry it; a
placeholder inside a longer string must be filled, by a single value. Keys are
never filled, so a payload can neither add an argument nor rename one, and a
field the template does not name is never sent.

What happened decides how a failure is classified, and the question asked is
whether `tools/call` was sent:

- before it was sent — the server could not be started or reached, it did not
  answer `initialize` in time, it closed before answering — the failure is
  `retryable`: nothing that could change anything was asked of it;
- after it was sent — a timeout, a server that exited or a stream that ended
  before answering, a server error — it is `ambiguous`, because the tool may
  have done its work;
- a result with `isError` is the system answering no, and is `terminal`; so is
  a protocol error saying the request itself was not acceptable.

The timeout of an operation bounds the whole exchange, not one read: over
stdio and over HTTP alike, the exchange has a deadline set when it opens. A
server that keeps a stream open with keep-alive comments is stopped there, and
a message whose deadline has passed is not sent at all.

The protocol defines no idempotency key, so none is sent. A write that ends
`ambiguous` is settled by its declared read-back, as every write is.
"""

from __future__ import annotations

import json
import queue
import re
import subprocess
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import IO, Any

import httpx

from oska import __version__
from oska.config import ResilienceConfig
from oska.connectors.base import (
    BaseConnector,
    ConnectorError,
    ConnectorOutcome,
    refused,
    rejected,
    succeeded,
    transport_failure,
    unanswered,
)
from oska.connectors.http import (
    _NEVER_LEFT,
    _NOT_SENDABLE,
    _read_prefix,
    _text,
    _TooLarge,
)
from oska.contracts.common import ConnectorResult

__all__ = [
    "CLIENT_NAME",
    "PROTOCOL_VERSION",
    "SUPPORTED_VERSIONS",
    "TRANSPORTS",
    "McpConnector",
]

CLIENT_NAME = "oska"
PROTOCOL_VERSION = "2025-06-18"
"""The protocol version this client asks for."""
SUPPORTED_VERSIONS = frozenset({"2024-11-05", "2025-03-26", "2025-06-18"})
"""The versions a server may answer with. Calling a tool means the same in each of them."""
TRANSPORTS = ("stdio", "http")
DEFAULT_ERROR_TEXT = 500
SERVER_ERROR = 500
SHUTDOWN_SECONDS = 1.0
"""How long a server started over stdio has to exit once its input is closed."""
MAX_TOOL_PAGES = 100
"""The most pages of `tools/list` a check reads before it stops asking."""

# Errors by which a server says the request itself was not acceptable, so nothing ran.
_REQUEST_ERRORS = frozenset({-32700, -32600, -32601, -32602})
_METHOD_NOT_FOUND = -32601
_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_WHOLE = re.compile(r"^\{([A-Za-z_][A-Za-z0-9_]*)\}$")
_SCALARS = (str, int, float, bool)
_OMITTED = object()
_EVENT_STREAM = "text/event-stream"


class _Stop(Exception):
    """An exchange ended without an answer to use; `outcome` says how."""

    def __init__(self, outcome: ConnectorOutcome) -> None:
        super().__init__(outcome.error)
        self.outcome = outcome


class _Expired(Exception):
    """An answer was still being read when the deadline of its exchange passed."""


def _lost(error: str, effects: bool, status_code: int | None = None) -> _Stop:
    """No usable answer: ambiguous once the call was sent, retryable before."""
    if effects:
        return _Stop(unanswered(error, status_code))
    return _Stop(transport_failure(error))


class _Session:
    """One exchange with a server, from `initialize` to the end of one call."""

    version: str | None = None

    def request(self, method: str, params: dict, *, effects: bool) -> dict:
        """Send a request and return the `result` of its answer, or raise `_Stop`."""
        raise NotImplementedError

    def notify(self, method: str) -> None:
        """Send a notification, which has no answer."""
        raise NotImplementedError

    def close(self) -> None:
        """Release the server, or the connection to it."""

    def initialize(self) -> None:
        """Agree on a protocol version and say the client is ready."""
        result = self.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": CLIENT_NAME, "version": __version__},
            },
            effects=False,
        )
        version = result.get("protocolVersion")
        if version not in SUPPORTED_VERSIONS:
            supported = ", ".join(sorted(SUPPORTED_VERSIONS))
            raise _Stop(
                refused(
                    f"the server speaks protocol version {version!r}, which this client "
                    f"does not; it speaks {supported}"
                )
            )
        self.version = str(version)
        self.notify("notifications/initialized")


def _answer(method: str, message: dict, effects: bool) -> dict:
    """The result of an answer, or the classified failure its error is."""
    if "error" in message:
        error = message.get("error")
        code = error.get("code") if isinstance(error, dict) else None
        text = error.get("message", "") if isinstance(error, dict) else str(error)
        reason = f"{method} was answered with error {code}: {text}"
        if code in _REQUEST_ERRORS or not effects:
            raise _Stop(refused(reason))
        raise _Stop(unanswered(reason))
    result = message.get("result")
    if not isinstance(result, dict):
        raise _lost(f"{method} was answered with a result that is not an object", effects)
    return result


class _StdioSession(_Session):
    """A server started for this exchange, spoken to over its input and output."""

    def __init__(
        self,
        argv: Sequence[str],
        environment: Mapping[str, str],
        cwd: Path | None,
        timeout: float,
        budget: int,
    ) -> None:
        self._timeout = timeout
        self._deadline = time.monotonic() + timeout
        self._budget = budget
        self._next_id = 0
        self._abandoned = False
        self._overflow = False
        self._lines: queue.Queue[bytes | None] = queue.Queue()
        try:
            # argv is a declared list, never a string, so no shell ever sees it.
            self._process = subprocess.Popen(
                list(argv),
                shell=False,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=dict(environment),
                cwd=cwd,
            )
        except (OSError, ValueError) as error:
            raise _Stop(
                transport_failure(f"{argv[0] if argv else 'the server'} could not be started: "
                                  f"{error}")
            ) from error
        stdout = self._process.stdout
        assert stdout is not None
        threading.Thread(target=self._read, args=(stdout,), daemon=True).start()

    def _read(self, stream: IO[bytes]) -> None:
        try:
            while True:
                line = stream.readline(self._budget + 1)
                if not line:
                    break
                if len(line) > self._budget and not line.endswith(b"\n"):
                    self._overflow = True
                    break
                self._lines.put(line)
        except (OSError, ValueError):
            pass
        self._lines.put(None)

    def _write(self, message: dict, what: str) -> None:
        stdin = self._process.stdin
        assert stdin is not None
        data = (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")
        try:
            stdin.write(data)
            stdin.flush()
        except (OSError, ValueError) as error:
            # The server no longer reads its input, so what was written cannot reach it.
            raise _Stop(
                transport_failure(f"the server stopped reading before {what} was sent: {error}")
            ) from error

    def request(self, method: str, params: dict, *, effects: bool) -> dict:
        self._next_id += 1
        ident = self._next_id
        self._write({"jsonrpc": "2.0", "id": ident, "method": method, "params": params}, method)
        while True:
            message = self._receive(method, effects)
            if message.get("id") == ident and ("result" in message or "error" in message):
                return _answer(method, message, effects)
            if "method" in message and "id" in message:
                # A request from the server: this client offers it nothing.
                self._write(
                    {
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "error": {"code": _METHOD_NOT_FOUND, "message": "not offered"},
                    },
                    "a reply to the server",
                )

    def notify(self, method: str) -> None:
        self._write({"jsonrpc": "2.0", "method": method}, method)

    def _receive(self, method: str, effects: bool) -> dict:
        while True:
            remaining = self._deadline - time.monotonic()
            try:
                if remaining <= 0:
                    raise queue.Empty
                line = self._lines.get(timeout=remaining)
            except queue.Empty:
                self._abandoned = True
                raise _lost(f"{method} timed out after {self._timeout}s", effects) from None
            if line is None:
                if self._overflow:
                    raise _Stop(
                        refused(f"the answer to {method} is larger than the budget of "
                                f"{self._budget} bytes, so it was not read")
                    )
                raise _lost(f"the server closed its output before answering {method}", effects)
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if isinstance(message, dict):
                return message

    def close(self) -> None:
        process = self._process
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        if not self._abandoned:
            try:
                process.wait(timeout=SHUTDOWN_SECONDS)
            except subprocess.TimeoutExpired:
                process.terminate()
        if process.poll() is None:
            process.kill()
        process.wait()
        if process.stdout is not None:
            process.stdout.close()


class _HttpSession(_Session):
    """An exchange over the streamable HTTP transport: one POST per message."""

    def __init__(
        self, client: httpx.Client, url: str, timeout: float, budget: int, error_limit: int
    ) -> None:
        self._client = client
        self._url = url
        self._timeout = timeout
        self._deadline = time.monotonic() + timeout
        self._budget = budget
        self._error_limit = error_limit
        self._next_id = 0
        self._session_id: str | None = None

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": f"application/json, {_EVENT_STREAM}",
            "Content-Type": "application/json",
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        if self.version:
            headers["MCP-Protocol-Version"] = self.version
        return headers

    def _send(self, message: dict, effects: bool) -> httpx.Response:
        what = str(message.get("method"))
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            # Nothing was sent, so nothing this message asks for can have happened.
            raise _Stop(transport_failure(f"{what} was not sent: {self._expired()}"))
        try:
            request = self._client.build_request(
                "POST", self._url, json=message, headers=self._headers(), timeout=remaining
            )
            response = self._client.send(request, stream=True)
        except _NEVER_LEFT as error:
            raise _Stop(transport_failure(f"{type(error).__name__}: {error}")) from error
        except _NOT_SENDABLE as error:
            raise _Stop(rejected(f"{type(error).__name__}: {error}")) from error
        except httpx.RequestError as error:
            raise _lost(f"{what}: {type(error).__name__}: {error}", effects) from error
        session = response.headers.get("mcp-session-id")
        if session:
            self._session_id = session
        status = response.status_code
        if status >= 400:
            try:
                text = _text(_read_prefix(response, self._error_limit), response.charset_encoding)
            except httpx.RequestError:
                text = ""
            finally:
                response.close()
            reason = f"{what}: http {status}: {text[: self._error_limit]}"
            if status >= SERVER_ERROR:
                raise _lost(reason, effects, status)
            raise _Stop(refused(reason, status))
        return response

    def request(self, method: str, params: dict, *, effects: bool) -> dict:
        self._next_id += 1
        ident = self._next_id
        message = {"jsonrpc": "2.0", "id": ident, "method": method, "params": params}
        response = self._send(message, effects)
        try:
            content_type = response.headers.get("content-type", "").split(";")[0].strip()
            chunks = _chunks(response, self._budget, self._deadline)
            if content_type.lower() == _EVENT_STREAM:
                messages: Iterator[Any] = (_parsed(data) for data in _events(chunks))
            else:
                messages = iter([_parsed(b"".join(chunks))])
            for candidate in messages:
                for item in candidate if isinstance(candidate, list) else [candidate]:
                    if (
                        isinstance(item, dict)
                        and item.get("id") == ident
                        and ("result" in item or "error" in item)
                    ):
                        return _answer(method, item, effects)
        except _TooLarge:
            raise _Stop(
                refused(f"the answer to {method} is larger than the budget of "
                        f"{self._budget} bytes, so it was not read")
            ) from None
        except _Expired:
            raise _lost(f"{method}: {self._expired()}", effects) from None
        except httpx.RequestError as error:
            raise _lost(f"{method}: {type(error).__name__}: {error}", effects) from error
        finally:
            response.close()
        raise _lost(f"the server ended its answer to {method} without one", effects)

    def notify(self, method: str) -> None:
        self._send({"jsonrpc": "2.0", "method": method}, effects=False).close()

    def _expired(self) -> str:
        return f"the exchange timed out after {self._timeout}s"

    def close(self) -> None:
        if not self._session_id:
            return
        try:
            self._client.delete(
                self._url, headers={"Mcp-Session-Id": self._session_id}, timeout=self._timeout
            )
        except httpx.HTTPError:
            pass


def _parsed(data: bytes | str) -> Any:
    try:
        return json.loads(data)
    except ValueError:
        return None


def _chunks(response: httpx.Response, budget: int, deadline: float) -> Iterator[bytes]:
    """The body as it arrives, stopped at the byte budget and at the deadline of the exchange.

    A read timeout bounds the wait for one chunk, not the whole answer, so a
    server that sends a keep-alive now and then would otherwise hold the call
    until the budget ran out. The deadline is checked as each chunk arrives.
    """
    declared = response.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > budget:
        raise _TooLarge
    size = 0
    for chunk in response.iter_bytes():
        size += len(chunk)
        if size > budget:
            raise _TooLarge
        if time.monotonic() >= deadline:
            raise _Expired
        yield chunk


def _events(chunks: Iterator[bytes]) -> Iterator[str]:
    """The data of each server-sent event in a body read chunk by chunk."""
    buffer = b""
    data: list[str] = []
    for chunk in chunks:
        buffer += chunk
        while (end := buffer.find(b"\n")) >= 0:
            line = buffer[:end].rstrip(b"\r").decode("utf-8", errors="replace")
            buffer = buffer[end + 1 :]
            if not line:
                if data:
                    yield "\n".join(data)
                    data = []
                continue
            field, _, value = line.partition(":")
            if field == "data":
                data.append(value[1:] if value.startswith(" ") else value)
    if data:
        yield "\n".join(data)


class McpConnector(BaseConnector):
    """Serves declared operations by calling one declared tool each on a protocol server.

    Exactly one of `argv` (the stdio transport) and `url` (the HTTP transport)
    is given. `environment` is the whole environment of a server started over
    stdio; `headers` are sent with every HTTP message.
    """

    def __init__(
        self,
        id: str,
        operations: dict[str, dict] | None = None,
        *,
        argv: Sequence[str] | None = None,
        environment: Mapping[str, str] | None = None,
        cwd: Path | str | None = None,
        url: str | None = None,
        headers: Mapping[str, str] | None = None,
        resilience: ResilienceConfig | None = None,
        http_transport: httpx.BaseTransport | None = None,
        error_text_limit: int = DEFAULT_ERROR_TEXT,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if (argv is None) == (url is None):
            raise ConnectorError(f"{id}: a tool server is started from argv or reached at url")
        super().__init__(id, resilience, sleep=sleep)
        self._operations = dict(operations or {})
        self._argv = list(argv or [])
        self._environment = dict(environment or {})
        self._cwd = Path(cwd) if cwd else None
        self._url = url
        self._error_limit = error_text_limit
        self._client: httpx.Client | None = None
        if url is not None:
            self._client = httpx.Client(headers=dict(headers or {}), transport=http_transport)

    def operations(self) -> list[str]:
        """Names of the operations this binding declares."""
        return sorted(self._operations)

    def close(self) -> None:
        """Release the connection pool of an HTTP binding."""
        if self._client is not None:
            self._client.close()

    def _open(self, timeout: float) -> _Session:
        budget = self.resilience.max_response_bytes
        if self._client is not None and self._url is not None:
            return _HttpSession(self._client, self._url, timeout, budget, self._error_limit)
        return _StdioSession(self._argv, self._environment, self._cwd, timeout, budget)

    def _perform(self, operation: str, payload: dict) -> ConnectorResult:
        """Call the tool declared for `operation` with the arguments its template builds."""
        spec = self._operations.get(operation)
        if spec is None:
            return rejected(f"undeclared operation {operation}")
        tool = spec.get("tool")
        if not isinstance(tool, str) or not tool:
            return rejected(f"{operation} declares no tool")
        try:
            arguments = _arguments(spec.get("arguments") or {}, payload)
        except ConnectorError as error:
            return rejected(str(error))
        timeout = float(spec.get("timeout", self.timeout))
        session: _Session | None = None
        try:
            session = self._open(timeout)
            session.initialize()
            result = session.request(
                "tools/call", {"name": tool, "arguments": arguments}, effects=True
            )
        except _Stop as stop:
            return stop.outcome
        finally:
            if session is not None:
                session.close()
        return _result_of(tool, result, self._error_limit)

    def unlisted_tools(self) -> list[tuple[str, str]]:
        """Each declared operation whose tool the server does not list, with that tool.

        This asks the server for its list, which a turn never does. A server
        that cannot be reached, or that answers with something other than a
        list, is a `ConnectorError`: the tools were not checked.
        """
        listed: set[str] = set()
        session: _Session | None = None
        try:
            session = self._open(self.timeout)
            session.initialize()
            cursor: str | None = None
            for _ in range(MAX_TOOL_PAGES):
                params = {"cursor": cursor} if cursor else {}
                page = session.request("tools/list", params, effects=False)
                tools = page.get("tools")
                if not isinstance(tools, list):
                    raise ConnectorError(f"{self.id}: tools/list was answered without a list")
                listed.update(
                    str(tool["name"])
                    for tool in tools
                    if isinstance(tool, dict) and "name" in tool
                )
                cursor = page.get("nextCursor") or None
                if cursor is None:
                    break
        except _Stop as stop:
            raise ConnectorError(f"{self.id}: {stop.outcome.error}") from None
        finally:
            if session is not None:
                session.close()
        return [
            (operation, str(spec.get("tool")))
            for operation, spec in sorted(self._operations.items())
            if spec.get("tool") not in listed
        ]


def _arguments(template: object, payload: dict) -> dict:
    """The arguments of a call: the template, its placeholders filled from the payload."""
    if not isinstance(template, dict):
        raise ConnectorError("the arguments template is not an object")
    filled = _fill(template, payload)
    assert isinstance(filled, dict)
    return filled


def _fill(value: object, payload: dict) -> object:
    if isinstance(value, str):
        whole = _WHOLE.match(value)
        if whole is not None:
            return payload.get(whole.group(1), _OMITTED)
        return _PLACEHOLDER.sub(lambda match: _scalar(match.group(1), payload), value)
    if isinstance(value, dict):
        filled = {key: _fill(item, payload) for key, item in value.items()}
        return {key: item for key, item in filled.items() if item is not _OMITTED}
    if isinstance(value, list):
        items = [_fill(item, payload) for item in value]
        return [item for item in items if item is not _OMITTED]
    return value


def _scalar(name: str, payload: dict) -> str:
    """One payload value as text inside a longer argument."""
    if name not in payload:
        raise ConnectorError(f"the payload carries no field {name}")
    raw = payload[name]
    if isinstance(raw, bool):
        return "true" if raw else "false"
    if not isinstance(raw, _SCALARS):
        raise ConnectorError(f"{name} is a {type(raw).__name__}, not a single value")
    return str(raw)


def _result_of(tool: str, result: dict, error_limit: int) -> ConnectorResult:
    """A tool result as an outcome: its error, its structured content, or its text."""
    content = result.get("content")
    items = content if isinstance(content, list) else []
    texts = [
        item["text"]
        for item in items
        if isinstance(item, dict)
        and item.get("type") == "text"
        and isinstance(item.get("text"), str)
    ]
    if result.get("isError") is True:
        reason = "\n".join(texts) or "the tool reported an error"
        return refused(f"{tool}: {reason[:error_limit]}")
    structured = result.get("structuredContent")
    if structured is not None:
        return succeeded(structured if isinstance(structured, dict) else {"data": structured})
    for text in texts:
        try:
            parsed = json.loads(text)
        except ValueError:
            continue
        return succeeded(parsed if isinstance(parsed, dict) else {"data": parsed})
    if texts:
        return succeeded({"text": "\n".join(texts)})
    return succeeded({})
