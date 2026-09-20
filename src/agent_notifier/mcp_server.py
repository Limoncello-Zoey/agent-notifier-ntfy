"""Dependency-free JSON-RPC MCP server over newline-delimited stdio."""

from __future__ import annotations

import json
import sys
from typing import Any, Callable, TextIO

from .client import ensure_daemon, notify_daemon
from .config import Config
from .errors import AgentNotifierError
from .notification import notification_from_mapping
from .sender import SendResult


SERVER_INFO = {"name": "agent-notifier", "version": "0.1.0"}
LEGACY_PROTOCOL = "2025-11-25"
MODERN_PROTOCOL = "2026-07-28"
SUPPORTED_PROTOCOLS = [MODERN_PROTOCOL, LEGACY_PROTOCOL, "2025-06-18", "2024-11-05"]
LEGACY_PROTOCOLS = SUPPORTED_PROTOCOLS[1:]
SERVER_META = {"io.modelcontextprotocol/serverInfo": SERVER_INFO}

TOOL = {
    "name": "ntfy_send",
    "description": "向已配置的 ntfy 话题或话题组发送任务状态通知。",
    "inputSchema": {
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "description": "已配置的话题或话题组名称；省略时使用默认目标。",
            },
            "emoji": {
                "type": "string",
                "minLength": 1,
                "description": "用于标题前缀的一个 emoji。",
            },
            "message": {
                "type": "string",
                "minLength": 1,
                "description": "通知正文。",
            },
            "title": {
                "type": "string",
                "minLength": 1,
                "description": "简短概括发生的事件。",
            },
            "priority": {
                "type": "integer",
                "enum": [4, 5],
                "description": "正常状态使用 4，严重失败或需要立即干预时使用 5。",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "可选附加 ntfy 标签。",
            },
        },
        "required": ["emoji", "title", "message", "priority"],
        "additionalProperties": False,
    },
    "annotations": {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
}


class MCPServer:
    def __init__(
        self,
        config: Config,
        *,
        notify: Callable[[Config, Any], SendResult] = notify_daemon,
    ) -> None:
        self.config = config
        self.notify = notify

    def handle(self, request: object) -> dict[str, Any] | None:
        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
            return _error(None, -32600, "Invalid Request")
        request_id = request.get("id")
        method = request.get("method")
        if not isinstance(method, str):
            return _error(request_id, -32600, "Invalid Request")
        if request_id is None:  # notifications never receive a response
            return None
        params = request.get("params", {})
        if not isinstance(params, dict):
            return _error(request_id, -32602, "Invalid params")

        if method == "server/discover":
            return _result(
                request_id,
                {
                    "supportedVersions": [MODERN_PROTOCOL],
                    "capabilities": {"tools": {}},
                    "instructions": "使用 ntfy_send 发送任务状态通知。",
                    "ttlMs": 0,
                    "cacheScope": "private",
                },
            )
        if method == "initialize":
            requested = params.get("protocolVersion")
            protocol = requested if requested in LEGACY_PROTOCOLS else LEGACY_PROTOCOL
            return _result(
                request_id,
                {
                    "protocolVersion": protocol,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": SERVER_INFO,
                    "instructions": "使用 ntfy_send 发送任务状态通知。",
                },
            )
        if method == "ping":
            return _result(request_id, {})
        if method == "tools/list":
            return _result(
                request_id,
                {"tools": [TOOL], "ttlMs": 0, "cacheScope": "private"},
            )
        if method == "tools/call":
            return self._call_tool(request_id, params)
        return _error(request_id, -32601, "Method not found")

    def _call_tool(self, request_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        if params.get("name") != "ntfy_send":
            return _error(request_id, -32602, "未知 Tool")
        arguments = params.get("arguments", {})
        if not isinstance(arguments, dict):
            return _error(request_id, -32602, "arguments 必须是对象")
        try:
            notification = notification_from_mapping(self.config, arguments)
            result = self.notify(self.config, notification)
        except AgentNotifierError as exc:
            return _result(
                request_id,
                {"content": [{"type": "text", "text": str(exc)}], "isError": True},
            )
        structured = result.to_dict()
        summary = (
            f"发送状态: {result.status}；成功 {result.sent}，失败 {result.failed}；"
            f"目标 {result.target}"
        )
        return _result(
            request_id,
            {
                "content": [{"type": "text", "text": summary}],
                "structuredContent": structured,
                "isError": result.status in {"failed", "partial_failure"},
            },
        )


def run_mcp(
    config: Config,
    *,
    instream: TextIO = sys.stdin,
    outstream: TextIO = sys.stdout,
    errstream: TextIO = sys.stderr,
    ensure: Callable[[Config], tuple[bool, str]] = ensure_daemon,
) -> None:
    ready, diagnostic = ensure(config)
    if not ready:
        print(f"agent-notifier: {diagnostic}", file=errstream, flush=True)
    server = MCPServer(config)
    for line in instream:
        try:
            request = json.loads(line)
            response = server.handle(request)
        except json.JSONDecodeError:
            response = _error(None, -32700, "Parse error")
        except Exception as exc:
            print(f"agent-notifier: MCP 内部错误: {exc}", file=errstream, flush=True)
            response = _error(None, -32603, "Internal error")
        if response is not None:
            outstream.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
            outstream.flush()


def _result(request_id: Any, result: object) -> dict[str, Any]:
    if isinstance(result, dict):
        result = {**result, "_meta": {**SERVER_META, **result.get("_meta", {})}}
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }
