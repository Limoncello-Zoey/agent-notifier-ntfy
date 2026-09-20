from dataclasses import replace
from io import StringIO
import json

from agent_notifier.config import Config, DefaultsConfig
from agent_notifier.mcp_server import MCPServer, TOOL, run_mcp
from agent_notifier.sender import SendResult


def config():
    return replace(Config(), defaults=DefaultsConfig("silent", 4), groups={"silent": []})


def request(method, params=None, request_id=1):
    value = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        value["params"] = params
    return value


def test_initialize_and_tools_list_expose_only_ntfy_send() -> None:
    server = MCPServer(config())
    initialized = server.handle(request("initialize", {"protocolVersion": "2025-11-25"}))
    assert initialized["result"]["protocolVersion"] == "2025-11-25"
    listed = server.handle(request("tools/list"))["result"]
    assert [item["name"] for item in listed["tools"]] == ["ntfy_send"]
    assert TOOL["inputSchema"]["required"] == ["emoji", "title", "message", "priority"]
    assert TOOL["inputSchema"]["additionalProperties"] is False
    assert TOOL["annotations"]["openWorldHint"] is True


def test_modern_discovery_advertises_current_and_legacy_protocols() -> None:
    result = MCPServer(config()).handle(request("server/discover"))["result"]
    assert result["supportedVersions"] == ["2026-07-28"]
    assert result["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "agent-notifier"


def test_tool_call_returns_structured_result() -> None:
    observed = []

    def notify(cfg, item):
        observed.append(item)
        return SendResult("skipped", "silent", (), 0, 0, ())

    response = MCPServer(config(), notify=notify).handle(
        request(
            "tools/call",
            {
                "name": "ntfy_send",
                "arguments": {"emoji": "✅", "title": "Done", "message": "Body", "priority": 4},
            },
        )
    )
    assert response["result"]["structuredContent"]["status"] == "skipped"
    assert response["result"]["isError"] is False
    assert observed[0].target is None


def test_tool_validation_failure_is_a_tool_error() -> None:
    response = MCPServer(config()).handle(
        request("tools/call", {"name": "ntfy_send", "arguments": {"message": "Body"}})
    )
    assert response["result"]["isError"] is True
    assert "缺少" in response["result"]["content"][0]["text"]


def test_notifications_have_no_response_and_unknown_methods_use_jsonrpc_error() -> None:
    server = MCPServer(config())
    assert server.handle(request("notifications/initialized", request_id=None)) is None
    assert server.handle(request("unknown"))["error"]["code"] == -32601


def test_stdio_keeps_stdout_json_only_and_reports_start_failure_to_stderr() -> None:
    incoming = StringIO(
        "not-json\n"
        + json.dumps(request("ping"))
        + "\n"
        + json.dumps(request("notifications/initialized", request_id=None))
        + "\n"
    )
    outgoing = StringIO()
    errors = StringIO()
    run_mcp(
        config(),
        instream=incoming,
        outstream=outgoing,
        errstream=errors,
        ensure=lambda cfg: (False, "service unavailable"),
    )
    lines = outgoing.getvalue().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["error"]["code"] == -32700
    assert json.loads(lines[1])["result"]["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "agent-notifier"
    assert "service unavailable" in errors.getvalue()
