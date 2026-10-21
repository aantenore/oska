"""The tool-protocol connector: declared operations bound to tools on a server.

The server is a small script written here and run with this interpreter, so
nothing leaves the machine; the HTTP transport is answered by a mock transport.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

from oska.config import ResilienceConfig
from oska.connectors.base import ConnectorError
from oska.connectors.mcp import PROTOCOL_VERSION, McpConnector
from oska.connectors.registry import ConnectorRegistry, check_bindings
from oska.kit.check import check_pack
from oska.kit.scaffold import new_pack

SERVER = r'''
import json
import os
import sys
import time
from pathlib import Path

state_path = Path(sys.argv[1])
mode = sys.argv[2] if len(sys.argv) > 2 else ""
log_path = state_path.with_suffix(".log")
TOOLS = ["get_item", "archive_item", "echo", "reject", "text_json", "plain_text", "slow", "env"]


def log(entry):
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")


def items():
    return json.loads(state_path.read_text(encoding="utf-8"))


def text(value):
    return [{"type": "text", "text": value}]


def call(name, arguments):
    if name == "get_item":
        for item in items()["items"]:
            if item["name"] == arguments.get("name"):
                found = {"item": item}
                return {"content": text(json.dumps(found)), "structuredContent": found}
        return {"content": text("no such item"), "isError": True}
    if name == "archive_item":
        state = items()
        for item in state["items"]:
            if item["name"] == arguments.get("name"):
                item["status"] = "archived"
                state_path.write_text(json.dumps(state), encoding="utf-8")
                return {"content": [], "structuredContent": {"item": item}}
        return {"content": text("no such item"), "isError": True}
    if name == "echo":
        return {"content": [], "structuredContent": {"arguments": arguments}}
    if name == "reject":
        return {"content": text("the item is locked"), "isError": True}
    if name == "text_json":
        return {"content": text("not json") + text('{"count": 2}')}
    if name == "plain_text":
        return {"content": text("all good")}
    if name == "slow":
        time.sleep(30)
        return {"content": []}
    if name == "env":
        return {"content": [], "structuredContent": {"names": sorted(os.environ)}}
    return None


def send(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


if mode == "exit":
    sys.exit(3)
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    log({"method": method, "params": message.get("params")})
    if "id" not in message:
        continue
    if method == "initialize":
        if mode == "hang":
            time.sleep(30)
        result = {
            "protocolVersion": message["params"]["protocolVersion"],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "test", "version": "1"},
        }
    elif method == "tools/list":
        result = {"tools": [{"name": name, "inputSchema": {"type": "object"}} for name in TOOLS]}
    else:
        result = call(message["params"]["name"], message["params"].get("arguments", {}))
    if result is None:
        error = {"code": -32602, "message": "unknown tool"}
        send({"jsonrpc": "2.0", "id": message["id"], "error": error})
    else:
        send({"jsonrpc": "2.0", "id": message["id"], "result": result})
'''

ITEMS = {"items": [{"name": "demo-1", "owner": "ops-team", "status": "active"}]}
QUICK = ResilienceConfig(timeout_seconds=1.0)


@pytest.fixture
def server(tmp_path: Path) -> Path:
    """The server script, with its state file beside it."""
    script = tmp_path / "server.py"
    script.write_text(SERVER, encoding="utf-8")
    (tmp_path / "state.json").write_text(json.dumps(ITEMS), encoding="utf-8")
    return script


def _calls(tmp_path: Path) -> list[dict]:
    log = tmp_path / "state.log"
    if not log.exists():
        return []
    lines = log.read_text(encoding="utf-8").splitlines()
    return [entry for entry in map(json.loads, lines) if entry["method"] == "tools/call"]


def _stdio(
    server: Path,
    operations: dict[str, dict],
    *,
    mode: str = "",
    resilience: ResilienceConfig | None = None,
    environment: dict[str, str] | None = None,
) -> McpConnector:
    argv = [sys.executable, str(server), str(server.parent / "state.json")]
    return McpConnector(
        "integration.tickets",
        operations,
        argv=[*argv, mode] if mode else argv,
        environment=environment,
        resilience=resilience,
    )


ECHO = {"tool": "echo", "arguments": {"id": "{ticket_id}"}}


# Over stdio


def test_a_call_maps_the_operation_to_its_tool_and_returns_structured_content(
    server: Path,
) -> None:
    connector = _stdio(server, {"item.get": {"tool": "get_item", "arguments": {"name": "{name}"}}})

    result = connector.execute("item.get", {"name": "demo-1"})

    assert result.ok, result.error
    assert result.data == {"item": ITEMS["items"][0]}
    log = (server.parent / "state.log").read_text(encoding="utf-8").splitlines()
    initialize = json.loads(log[0])
    assert initialize["method"] == "initialize"
    assert initialize["params"]["protocolVersion"] == PROTOCOL_VERSION
    assert initialize["params"]["clientInfo"]["name"] == "oska"
    assert json.loads(log[1])["method"] == "notifications/initialized"


def test_text_content_is_read_as_json_when_it_parses_and_kept_as_text_otherwise(
    server: Path,
) -> None:
    connector = _stdio(
        server, {"count": {"tool": "text_json"}, "status": {"tool": "plain_text"}}
    )

    assert connector.execute("count", {}).data == {"count": 2}
    assert connector.execute("status", {}).data == {"text": "all good"}


def test_a_tool_error_is_the_system_saying_no(server: Path) -> None:
    connector = _stdio(server, {"ticket.close": {"tool": "reject"}})

    result = connector.execute("ticket.close", {}, write=True)

    assert (result.ok, result.failure, result.classification) == (False, "refused", "terminal")
    assert "the item is locked" in (result.error or "")
    assert connector.health.consecutive_failures == 0


def test_a_tool_the_server_does_not_know_is_refused_and_listed_as_missing(server: Path) -> None:
    connector = _stdio(server, {"ticket.get": ECHO, "ticket.purge": {"tool": "purge"}})

    result = connector.execute("ticket.purge", {})

    assert (result.failure, result.classification) == ("refused", "terminal")
    assert connector.unlisted_tools() == [("ticket.purge", "purge")]


def test_an_undeclared_operation_is_rejected_without_starting_the_server(server: Path) -> None:
    connector = _stdio(server, {"ticket.get": ECHO})

    result = connector.execute("echo", {"ticket_id": "T-1"})

    assert (result.failure, result.classification) == ("rejected", "terminal")
    assert result.error == "undeclared operation echo"
    assert not (server.parent / "state.log").exists()


def test_arguments_are_filled_field_by_field_and_nothing_else_is_sent(server: Path) -> None:
    template = {
        "id": "{ticket_id}",
        "labels": "{labels}",
        "note": "ticket {ticket_id} by {owner}",
        "fixed": 3,
        "nested": {"who": "{owner}", "optional": "{absent}"},
    }
    connector = _stdio(server, {"ticket.echo": {"tool": "echo", "arguments": template}})

    result = connector.execute(
        "ticket.echo",
        {"ticket_id": "T-1", "labels": ["a", "b"], "owner": "ops", "admin": True},
    )

    assert result.data == {
        "arguments": {
            "id": "T-1",
            "labels": ["a", "b"],
            "note": "ticket T-1 by ops",
            "fixed": 3,
            "nested": {"who": "ops"},
        }
    }


def test_a_payload_value_cannot_add_or_rename_an_argument(server: Path) -> None:
    template = {"id": "{ticket_id}", "note": "about {ticket_id}"}
    connector = _stdio(server, {"ticket.echo": {"tool": "echo", "arguments": template}})
    hostile = '{owner}", "admin": true, "x": "'

    result = connector.execute("ticket.echo", {"ticket_id": hostile, "owner": "root"})

    assert result.data == {"arguments": {"id": hostile, "note": f"about {hostile}"}}


def test_a_placeholder_inside_text_needs_one_value_and_is_rejected_without_one(
    server: Path,
) -> None:
    connector = _stdio(
        server, {"ticket.echo": {"tool": "echo", "arguments": {"note": "about {ticket_id}"}}}
    )

    missing = connector.execute("ticket.echo", {})
    composite = connector.execute("ticket.echo", {"ticket_id": {"nested": 1}})

    assert (missing.failure, composite.failure) == ("rejected", "rejected")
    assert not (server.parent / "state.log").exists()


def test_a_call_that_times_out_after_it_was_sent_is_ambiguous(server: Path) -> None:
    connector = _stdio(server, {"ticket.slow": {"tool": "slow"}}, resilience=QUICK)

    result = connector.execute("ticket.slow", {}, write=True)

    assert (result.failure, result.classification) == ("transport", "ambiguous")
    assert "timed out" in (result.error or "")


def test_a_server_that_does_not_answer_initialize_is_retryable(server: Path) -> None:
    connector = _stdio(server, {"ticket.get": ECHO}, mode="hang", resilience=QUICK)

    result = connector.execute("ticket.get", {"ticket_id": "T-1"}, write=True)

    assert (result.failure, result.classification) == ("transport", "retryable")
    assert _calls(server.parent) == []


@pytest.mark.parametrize("mode", ["exit", "missing"])
def test_a_server_that_cannot_start_is_retryable(server: Path, mode: str) -> None:
    connector = (
        McpConnector("integration.tickets", {"ticket.get": ECHO}, argv=[str(server.parent / "x")])
        if mode == "missing"
        else _stdio(server, {"ticket.get": ECHO}, mode=mode)
    )

    result = connector.execute("ticket.get", {"ticket_id": "T-1"}, write=True)

    assert (result.failure, result.classification) == ("transport", "retryable")
    assert connector.health.consecutive_failures == 1


def test_the_server_sees_only_the_search_path_and_what_the_binding_declares(
    server: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OSKA_TEST_SECRET", "not for the server")
    monkeypatch.setenv("OSKA_TEST_TOKEN", "for the server")
    bindings = {
        "integrations": {
            "integration.tickets": {
                "type": "mcp",
                "transport": "stdio",
                "argv": [sys.executable, str(server), str(tmp_path / "state.json")],
                "env": {"TICKETS_TOKEN": "${OSKA_TEST_TOKEN}"},
                "operations": {"ticket.env": {"tool": "env"}},
            }
        }
    }

    registry = ConnectorRegistry.from_document(bindings, tmp_path)
    names = registry.get("integration.tickets").execute("ticket.env", {}).data["names"]

    assert "TICKETS_TOKEN" in names
    assert "OSKA_TEST_SECRET" not in names
    assert "OSKA_TEST_TOKEN" not in names


# The binding


def test_a_binding_reads_only_the_environment_it_is_granted(tmp_path: Path) -> None:
    binding = {
        "type": "mcp",
        "transport": "stdio",
        "argv": ["${SERVER_BIN}", "--serve"],
        "operations": {"ticket.get": ECHO},
    }

    with pytest.raises(ConnectorError, match="SERVER_BIN"):
        check_bindings({"integrations": {"i": binding}}, tmp_path, readable=lambda _: False)
    check_bindings({"integrations": {"i": binding}}, tmp_path, readable=lambda n: True)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"transport": "websocket"}, "transport"),
        ({"argv": "server --serve"}, "argv"),
        ({"operations": {"ticket.get": {"arguments": {}}}}, "declares no tool"),
        ({"operations": {"ticket.get": {"tool": "echo", "arguments": ["{x}"]}}}, "arguments"),
        ({"transport": "http"}, "no url"),
    ],
)
def test_a_binding_without_the_shape_of_its_transport_is_refused(
    tmp_path: Path, change: dict, message: str
) -> None:
    binding = {
        "type": "mcp",
        "transport": "stdio",
        "argv": ["server"],
        "operations": {"ticket.get": ECHO},
        **change,
    }

    with pytest.raises(ConnectorError, match=message):
        ConnectorRegistry.from_document({"integrations": {"i": binding}}, tmp_path)


# Over HTTP


def _http_server(
    seen: list[httpx.Request], call: httpx.Response | Exception, *, init_status: int = 200
) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "DELETE":
            return httpx.Response(200)
        method = json.loads(request.content)["method"]
        if method == "initialize":
            if init_status != 200:
                return httpx.Response(init_status, text="unavailable")
            result = {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}}
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": 1, "result": result},
                headers={"Mcp-Session-Id": "session-1"},
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        if isinstance(call, Exception):
            raise call
        return call

    return httpx.MockTransport(handle)


def _http(transport: httpx.MockTransport) -> McpConnector:
    return McpConnector(
        "integration.tickets",
        {"ticket.get": ECHO},
        url="https://tools.example/mcp",
        headers={"Authorization": "Bearer token"},
        http_transport=transport,
    )


def test_an_answer_streamed_as_events_is_read_to_the_matching_response() -> None:
    notice = {"jsonrpc": "2.0", "method": "notifications/progress", "params": {}}
    answer = {
        "jsonrpc": "2.0",
        "id": 2,
        "result": {"content": [], "structuredContent": {"ticket": "T-1"}},
    }
    stream = f"event: message\ndata: {json.dumps(notice)}\n\ndata: {json.dumps(answer)}\n\n"
    seen: list[httpx.Request] = []
    response = httpx.Response(
        200, content=stream.encode(), headers={"Content-Type": "text/event-stream"}
    )

    result = _http(_http_server(seen, response)).execute("ticket.get", {"ticket_id": "T-1"})

    assert result.ok, result.error
    assert result.data == {"ticket": "T-1"}
    call = seen[2]
    assert json.loads(call.content)["params"] == {"name": "echo", "arguments": {"id": "T-1"}}
    assert call.headers["Mcp-Session-Id"] == "session-1"
    assert call.headers["MCP-Protocol-Version"] == PROTOCOL_VERSION
    assert call.headers["Authorization"] == "Bearer token"
    assert "text/event-stream" in call.headers["Accept"]
    assert seen[-1].method == "DELETE"


def test_a_json_answer_is_read_as_the_result() -> None:
    answer = {"jsonrpc": "2.0", "id": 2, "result": {"content": [], "structuredContent": {"n": 1}}}
    result = _http(_http_server([], httpx.Response(200, json=answer))).execute(
        "ticket.get", {"ticket_id": "T-1"}
    )
    assert result.data == {"n": 1}


@pytest.mark.parametrize(
    ("call", "init_status", "classification"),
    [
        (httpx.ConnectError("refused"), 200, "retryable"),
        (httpx.ReadTimeout("slow"), 200, "ambiguous"),
        (httpx.Response(503, text="down"), 200, "ambiguous"),
        (httpx.Response(200, json={}), 503, "retryable"),
    ],
)
def test_http_failures_are_classified_by_whether_the_call_was_sent(
    call: httpx.Response | Exception, init_status: int, classification: str
) -> None:
    connector = _http(_http_server([], call, init_status=init_status))

    result = connector.execute("ticket.get", {"ticket_id": "T-1"}, write=True)

    assert (result.failure, result.classification) == ("transport", classification)


# Checked as a pack, and run through the engine


def _bind_to_server(target: Path, server: Path, archive_tool: str = "archive_item") -> None:
    path = target / "configs" / "connectors.json"
    bindings = json.loads(path.read_text(encoding="utf-8"))
    bindings["integrations"]["integration.core"] = {
        "type": "mcp",
        "transport": "stdio",
        "argv": [sys.executable, str(server), str(server.parent / "state.json")],
        "operations": {
            "item.get": {"tool": "get_item", "arguments": {"name": "{name}"}},
            "item.archive": {"tool": archive_tool, "arguments": {"name": "{name}"}},
        },
    }
    path.write_text(json.dumps(bindings), encoding="utf-8")


def test_a_pack_bound_to_a_tool_server_runs_the_write_path_through_the_engine(
    server: Path, tmp_path: Path
) -> None:
    target = new_pack("sample_pack", into=tmp_path / "packs")
    _bind_to_server(target, server)

    refused = check_pack(target)
    assert any("--allow-shell" in error for error in refused.errors)
    assert not (tmp_path / "state.log").exists(), "a refused tool server was started"

    report = check_pack(target, allow_shell=True)

    assert report.ok, (report.errors, report.scenarios)
    archive = [call for call in _calls(tmp_path) if call["params"]["name"] == "archive_item"]
    # Approved once and executed once; the repeat in the same session is a ledger hit.
    assert [call["params"]["arguments"] for call in archive] == [{"name": "demo-1"}]
    reads = [call for call in _calls(tmp_path) if call["params"]["name"] == "get_item"]
    assert len(reads) >= 2, "the write was not read back through the other tool"
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert state["items"][0]["status"] == "archived"


def test_a_pack_check_refuses_an_operation_bound_to_a_tool_the_server_lacks(
    server: Path, tmp_path: Path
) -> None:
    target = new_pack("sample_pack", into=tmp_path / "packs")
    _bind_to_server(target, server, archive_tool="archive_everything")

    report = check_pack(target, allow_shell=True)

    assert not report.ok
    assert any(
        "item.archive" in error and "archive_everything" in error for error in report.errors
    ), report.errors


def test_a_pack_check_notes_a_tool_server_it_cannot_reach(tmp_path: Path) -> None:
    target = new_pack("sample_pack", into=tmp_path / "packs")
    path = target / "configs" / "connectors.json"
    bindings = json.loads(path.read_text(encoding="utf-8"))
    bindings["integrations"]["integration.unreached"] = {
        "type": "mcp",
        "transport": "http",
        "url": "http://127.0.0.1:9/mcp",
        "resilience": {"timeout_seconds": 1.0},
        "operations": {"item.get": {"tool": "get_item"}},
    }
    path.write_text(json.dumps(bindings), encoding="utf-8")

    report = check_pack(target)

    notes = [issue for issue in report.validation_issues if issue.code == "tools_unchecked"]
    assert [note.severity for note in notes] == ["warning"]
    assert notes[0].skill_id == "integration.unreached"
