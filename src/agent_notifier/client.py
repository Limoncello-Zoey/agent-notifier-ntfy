"""Local daemon HTTP client used by CLI and MCP ingress processes."""

from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import Config
from .errors import DaemonError
from .notification import Notification
from .sender import SendResult, TopicResult


def daemon_url(config: Config, path: str) -> str:
    host = f"[{config.monitor.listen_host}]" if ":" in config.monitor.listen_host else config.monitor.listen_host
    return f"http://{host}:{config.monitor.listen_port}{path}"


def notify_daemon(config: Config, notification: Notification) -> SendResult:
    payload = _request(
        config,
        "/v1/notify",
        notification.to_dict(),
        timeout=config.monitor.request_timeout_seconds + 2,
    )
    try:
        results = tuple(
            TopicResult(
                topic=item["topic"],
                ok=item["ok"],
                message_id=item.get("message_id"),
                returncode=item.get("returncode"),
                error=item.get("error"),
            )
            for item in payload["results"]
        )
        return SendResult(
            payload["status"],
            payload["target"],
            tuple(payload["resolved_topics"]),
            int(payload["sent"]),
            int(payload["failed"]),
            results,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise DaemonError("守护进程返回了无效的发送结果") from exc


def daemon_health(config: Config, timeout: float = 2.0) -> dict[str, Any]:
    request = Request(daemon_url(config, "/health"), method="GET")
    return _open_json(request, timeout)


def _request(config: Config, path: str, payload: object, timeout: float) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request = Request(
        daemon_url(config, path),
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    return _open_json(request, timeout)


def _open_json(request: Request, timeout: float) -> dict[str, Any]:
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except HTTPError as exc:
        try:
            detail = json.load(exc).get("error")
        except Exception:
            detail = None
        raise DaemonError(detail or f"守护进程返回 HTTP {exc.code}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise DaemonError(f"无法连接 agent-notifierd: {exc}") from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise DaemonError("守护进程返回了无效 JSON") from exc
    if not isinstance(payload, dict):
        raise DaemonError("守护进程返回的 JSON 不是对象")
    return payload
