"""Serial-friendly ntfy CLI invocation and structured result parsing."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import shutil
import subprocess
import time
from typing import Any, Callable, Sequence
from urllib.parse import quote

from .config import Config
from .errors import NotificationError
from .notification import Notification
from .resolver import resolve_target


MAX_ERROR_CHARS = 500


@dataclass(frozen=True, slots=True)
class TopicResult:
    topic: str
    ok: bool
    message_id: str | None = None
    returncode: int | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value is not None}


@dataclass(frozen=True, slots=True)
class SendResult:
    status: str
    target: str
    resolved_topics: tuple[str, ...]
    sent: int
    failed: int
    results: tuple[TopicResult, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "target": self.target,
            "resolved_topics": list(self.resolved_topics),
            "sent": self.sent,
            "failed": self.failed,
            "results": [item.to_dict() for item in self.results],
        }


Runner = Callable[..., subprocess.CompletedProcess[str]]


class NtfySender:
    def __init__(
        self,
        config: Config,
        *,
        executable: str | None = None,
        timeout: int = 30,
        max_attempts: int = 3,
        runner: Runner = subprocess.run,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.executable = executable
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.runner = runner
        self.sleeper = sleeper

    def send(self, notification: Notification) -> SendResult:
        target, topics = resolve_target(self.config, notification.target)
        if not topics:
            return SendResult("skipped", target, (), 0, 0, ())
        executable = self.executable or shutil.which("ntfy")
        if not executable:
            raise NotificationError("找不到 ntfy 可执行文件；请安装 ntfy CLI 并确认其位于 PATH")

        results = tuple(
            self._send_topic(executable, topic, notification) for topic in topics
        )
        sent = sum(item.ok for item in results)
        failed = len(results) - sent
        status = "success" if failed == 0 else "failed" if sent == 0 else "partial_failure"
        return SendResult(status, target, tuple(topics), sent, failed, results)

    def _send_topic(
        self, executable: str, topic: str, notification: Notification
    ) -> TopicResult:
        result: TopicResult | None = None
        for attempt in range(self.max_attempts):
            result, retryable = self._attempt(executable, topic, notification)
            if result.ok or not retryable or attempt + 1 == self.max_attempts:
                return result
            self.sleeper(float(2**attempt))
        assert result is not None
        return result

    def _attempt(
        self, executable: str, topic: str, notification: Notification
    ) -> tuple[TopicResult, bool]:
        command = self._command(executable, topic, notification)
        try:
            completed = self.runner(
                command,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return TopicResult(topic, False, error=f"ntfy 子进程超过 {self.timeout} 秒"), False
        except OSError as exc:
            return TopicResult(topic, False, error=_bounded(str(exc))), False

        payload = _parse_json(completed.stdout) or _parse_json(completed.stderr)
        server_error = _server_error(payload)
        http_status = _http_status(payload)
        if completed.returncode != 0 or server_error is not None:
            detail = server_error or completed.stderr.strip() or completed.stdout.strip()
            detail = _bounded(detail or f"ntfy 退出码 {completed.returncode}")
            retryable = http_status == 429 or (http_status is not None and 500 <= http_status <= 599)
            return TopicResult(topic, False, returncode=completed.returncode, error=detail), retryable
        if not payload or payload.get("event") != "message" or not isinstance(payload.get("id"), str):
            return (
                TopicResult(
                    topic,
                    False,
                    returncode=completed.returncode,
                    error="ntfy 未返回包含 event=message 和消息 ID 的有效 JSON",
                ),
                False,
            )
        return TopicResult(topic, True, str(payload["id"]), completed.returncode), False

    def _command(
        self, executable: str, topic: str, notification: Notification
    ) -> list[str]:
        command = [
            executable,
            "publish",
            "--title",
            notification.rendered_title,
            "--priority",
            str(notification.priority),
        ]
        if notification.tags:
            command.extend(["--tags", ",".join(notification.tags)])
        url = f"{self.config.server.base_url}/{quote(topic, safe='')}"
        command.extend([url, notification.message])
        return command


def _parse_json(value: str) -> dict[str, Any] | None:
    text = value.strip()
    if not text:
        return None
    for line in reversed(text.splitlines()):
        try:
            parsed = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _server_error(payload: dict[str, Any] | None) -> str | None:
    if not payload:
        return None
    if any(key in payload for key in ("code", "http", "error")):
        value = payload.get("error") or payload.get("code") or payload.get("http")
        return _bounded(str(value))
    return None


def _http_status(payload: dict[str, Any] | None) -> int | None:
    if not payload:
        return None
    for key in ("http", "code"):
        value = payload.get(key)
        if isinstance(value, int) and 100 <= value <= 599:
            return value
        if isinstance(value, str) and value.isdigit() and 100 <= int(value) <= 599:
            return int(value)
    return None


def _bounded(value: str) -> str:
    cleaned = " ".join(value.split())
    return cleaned[:MAX_ERROR_CHARS]
