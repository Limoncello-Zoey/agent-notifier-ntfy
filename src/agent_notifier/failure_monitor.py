"""Grace-window and de-duplication state machine for Codex transport failures."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import logging
import re
import threading
import time
from typing import Callable

from .config import Config
from .notification import Notification, normalize_notification
from .otlp_receiver import TransportEvent
from .sender import SendResult


LOGGER = logging.getLogger("agent_notifier.failure_monitor")


TEMPLATES = {
    "authentication_failed": ("🔐", "模型认证失败"),
    "rate_limited": ("⏳", "模型服务限流"),
    "server_unavailable": ("🚨", "模型服务异常"),
    "connection_failed": ("📡", "模型服务连接失败"),
    "response_stream_disconnected": ("🔌", "模型响应流中断"),
    "transport_failure": ("⚠️", "模型通信异常"),
}
TRANSPORT_NAMES = {"api": "API", "sse": "SSE", "websocket": "WebSocket"}
CONNECTION_WORDS = re.compile(
    r"\b(dns|tcp|tls|connect(?:ion)?|resolve|socket|network|handshake|certificate)\b",
    re.IGNORECASE,
)
SECRET_PATTERNS = (
    re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)\b(authorization|api[_-]?key|token|password)\s*[:=]\s*[^\s,;]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(
        r"(?i)\b(user[_ -]?prompt|prompt|model[_ -]?output|output|request[_ -]?body)"
        r"\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^,;]+)"
    ),
)


@dataclass(slots=True)
class _Pending:
    first_seen: float
    event: TransportEvent
    category: str
    fingerprint: str


class FailureMonitor:
    def __init__(
        self,
        config: Config,
        enqueue: Callable[
            [Notification, Callable[[SendResult | None, BaseException | None], None]],
            bool,
        ],
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.enqueue = enqueue
        self.clock = clock
        self._pending: dict[tuple[str, str], _Pending] = {}
        self._dedupe: dict[tuple[str, str, str], float] = {}
        self._inflight: dict[tuple[str, str], object] = {}
        self._lock = threading.RLock()

    def ingest(self, event: TransportEvent, now: float | None = None) -> None:
        timestamp = self.clock() if now is None else now
        key = (event.session_id, event.transport)
        with self._lock:
            self._prune(timestamp)
            if event.success:
                self._pending.pop(key, None)
                for dedupe_key in list(self._dedupe):
                    if dedupe_key[:2] == key:
                        del self._dedupe[dedupe_key]
                return

            category = classify_failure(event)
            fingerprint = _fingerprint(category, event)
            previous = self._pending.get(key)
            first_seen = previous.first_seen if previous is not None else timestamp
            self._pending[key] = _Pending(first_seen, event, category, fingerprint)

    def tick(self, now: float | None = None) -> int:
        timestamp = self.clock() if now is None else now
        enqueued = 0
        with self._lock:
            self._prune(timestamp)
            for key, pending in list(self._pending.items()):
                if key in self._inflight:
                    continue
                if timestamp - pending.first_seen < self.config.monitor.failure_grace_seconds:
                    continue
                dedupe_key = (*key, pending.fingerprint)
                previous = self._dedupe.get(dedupe_key)
                if previous is not None and timestamp - previous < self.config.monitor.dedupe_window_seconds:
                    del self._pending[key]
                    continue
                notification = build_notification(self.config, pending.event, pending.category)
                token = object()
                self._inflight[key] = token
                callback = lambda result, error, key=key, token=token, pending=pending: (
                    self._delivery_complete(key, token, pending, result, error)
                )
                # Enqueue remains non-blocking; completion decides whether the
                # alert was actually delivered and may be de-duplicated.
                if self.enqueue(notification, callback):
                    enqueued += 1
                elif self._inflight.get(key) is token:
                    del self._inflight[key]
        return enqueued

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    def _prune(self, now: float) -> None:
        window = self.config.monitor.dedupe_window_seconds
        for key, sent_at in list(self._dedupe.items()):
            if now - sent_at >= window:
                del self._dedupe[key]

    def _delivery_complete(
        self,
        key: tuple[str, str],
        token: object,
        submitted: _Pending,
        result: SendResult | None,
        error: BaseException | None,
    ) -> None:
        delivered = (
            error is None
            and result is not None
            and result.sent > 0
            and result.status in {"success", "partial_failure"}
        )
        timestamp = self.clock()
        with self._lock:
            if self._inflight.get(key) is token:
                del self._inflight[key]
            pending = self._pending.get(key)
            if pending is submitted and delivered:
                del self._pending[key]
                self._dedupe[(*key, submitted.fingerprint)] = timestamp
            elif pending is submitted:
                # Keep the alert pending, but avoid a tight retry loop while the
                # notification destination is unavailable.
                pending.first_seen = timestamp

        if error is not None:
            LOGGER.error("后台模型故障通知发送失败: %s", _bounded(str(error)))
        elif not delivered:
            status = result.status if result is not None else "missing_result"
            sent = result.sent if result is not None else 0
            failed = result.failed if result is not None else 0
            LOGGER.error(
                "后台模型故障通知未送达: status=%s sent=%s failed=%s",
                status,
                sent,
                failed,
            )
        elif result is not None and result.status == "partial_failure":
            LOGGER.warning(
                "后台模型故障通知部分送达: sent=%s failed=%s",
                result.sent,
                result.failed,
            )


def classify_failure(event: TransportEvent) -> str:
    if event.status in (401, 403):
        return "authentication_failed"
    if event.status == 429:
        return "rate_limited"
    if event.status is not None and 500 <= event.status <= 599:
        return "server_unavailable"
    if event.error and CONNECTION_WORDS.search(event.error):
        return "connection_failed"
    if event.transport in ("sse", "websocket"):
        return "response_stream_disconnected"
    return "transport_failure"


def build_notification(config: Config, event: TransportEvent, category: str) -> Notification:
    emoji, title = TEMPLATES[category]
    lines: list[str] = []
    if event.model:
        lines.append(f"模型：{_sanitize(event.model, 100)}")
    lines.append(f"通道：{TRANSPORT_NAMES.get(event.transport, _sanitize(event.transport, 40))}")
    if event.status is not None:
        lines.append(f"状态：HTTP {event.status}")
    else:
        lines.append("状态：传输失败")
    if event.attempt is not None:
        lines.append(f"尝试：{event.attempt}")
    if event.error:
        lines.append(f"错误：{_sanitize(event.error, 240)}")
    return normalize_notification(
        config,
        target=None,
        emoji=emoji,
        title=title,
        message="\n".join(lines),
        priority=5,
        tags=(),
    )


def _sanitize(value: str, limit: int) -> str:
    result = " ".join(value.replace("\x00", "").split())
    for pattern in SECRET_PATTERNS:
        result = pattern.sub(lambda match: f"{match.group(1)} [REDACTED]" if match.lastindex else "[REDACTED]", result)
    return result[:limit]


def _fingerprint(category: str, event: TransportEvent) -> str:
    stable = f"{category}|{event.status or ''}|{_sanitize(event.error or '', 240)}"
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()[:16]


def _bounded(value: str) -> str:
    return " ".join(value.split())[:500]
