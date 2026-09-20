"""Loopback HTTP daemon combining notification queue and OTLP monitoring."""

from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import socket
import threading
from typing import Any, Callable

from .config import Config
from .errors import AgentNotifierError, NotificationError
from .failure_monitor import FailureMonitor
from .notification import notification_from_mapping
from .otlp_receiver import OtlpDecodeError, decode_otlp_logs
from .send_queue import QueueFullError, QueueTimeoutError, SendQueue
from .sender import NtfySender


LOGGER = logging.getLogger("agent_notifier.daemon")
NOTIFY_BODY_LIMIT = 16 * 1024
OTLP_BODY_LIMIT = 1024 * 1024
QUEUE_CAPACITY = 100
NOTIFY_TIMEOUT_SECONDS = 60


class NotifierHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class DaemonRuntime:
    def __init__(self, config: Config, *, sender: NtfySender | None = None) -> None:
        self.config = config
        actual_sender = sender or NtfySender(config)
        self.queue = SendQueue(actual_sender.send, QUEUE_CAPACITY)
        self.monitor = FailureMonitor(config, self.queue.submit_background)
        server_class = _server_class(config.monitor.listen_host)
        self.server = server_class(
            (config.monitor.listen_host, config.monitor.listen_port),
            _handler_factory(self),
        )
        self._stop = threading.Event()
        self._ticker = threading.Thread(target=self._tick, name="agent-notifier-monitor", daemon=True)
        self._cleanup_lock = threading.Lock()
        self._cleaned = False

    def serve_forever(self) -> None:
        self.queue.start()
        self._ticker.start()
        LOGGER.info(
            "监听 http://%s:%s", self.server.server_address[0], self.server.server_address[1]
        )
        try:
            self.server.serve_forever()
        finally:
            self._cleanup()

    def start_in_thread(self) -> threading.Thread:
        thread = threading.Thread(target=self.serve_forever, name="agent-notifier-http", daemon=True)
        thread.start()
        return thread

    def close(self) -> None:
        self._stop.set()
        self.server.shutdown()
        self._cleanup()

    def _cleanup(self) -> None:
        with self._cleanup_lock:
            if self._cleaned:
                return
            self._cleaned = True
        self.server.server_close()
        self.queue.shutdown()

    def _tick(self) -> None:
        interval = min(1.0, max(0.1, self.config.monitor.failure_grace_seconds / 2))
        while not self._stop.wait(interval):
            try:
                self.monitor.tick()
            except Exception as exc:  # keep the monitor alive; never log raw events
                LOGGER.error("故障监测定时任务失败: %s", _bounded(str(exc)))


def run_daemon(config: Config) -> None:
    DaemonRuntime(config).serve_forever()


def _server_class(host: str) -> type[NotifierHTTPServer]:
    if ":" not in host:
        return NotifierHTTPServer

    class IPv6NotifierHTTPServer(NotifierHTTPServer):
        address_family = socket.AF_INET6

    return IPv6NotifierHTTPServer


def _handler_factory(runtime: DaemonRuntime) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "AgentNotifier/0.1"

        def do_GET(self) -> None:
            if self.path == "/health":
                self._json(HTTPStatus.OK, {"status": "ok", "queue_pending": runtime.queue.pending})
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "端点不存在"})

        def do_POST(self) -> None:
            if self.path == "/v1/notify":
                self._notify()
            elif self.path == "/v1/logs":
                self._logs()
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "端点不存在"})

        def do_PUT(self) -> None:
            self._json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "不支持的 HTTP 方法"})

        do_DELETE = do_PUT
        do_PATCH = do_PUT

        def _notify(self) -> None:
            try:
                payload = self._read_json(NOTIFY_BODY_LIMIT)
                if not isinstance(payload, dict):
                    raise NotificationError("通知请求必须是 JSON 对象")
                notification = notification_from_mapping(runtime.config, payload)
                result = runtime.queue.submit(
                    notification, NOTIFY_TIMEOUT_SECONDS
                )
            except QueueFullError as exc:
                self._json(HTTPStatus.TOO_MANY_REQUESTS, {"error": str(exc)})
            except QueueTimeoutError as exc:
                self._json(HTTPStatus.GATEWAY_TIMEOUT, {"error": str(exc)})
            except (NotificationError, ValueError) as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": _bounded(str(exc))})
            except AgentNotifierError as exc:
                self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": _bounded(str(exc))})
            except Exception as exc:
                LOGGER.error("发送请求失败: %s", _bounded(str(exc)))
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "发送请求处理失败"})
            else:
                self._json(HTTPStatus.OK, result.to_dict())

        def _logs(self) -> None:
            try:
                payload = self._read_json(OTLP_BODY_LIMIT)
                events = decode_otlp_logs(payload)
                for event in events:
                    runtime.monitor.ingest(event)
            except (OtlpDecodeError, ValueError) as exc:
                LOGGER.warning("拒绝无效 OTLP 请求: %s", _bounded(str(exc)))
                self._json(HTTPStatus.BAD_REQUEST, {"error": _bounded(str(exc))})
            else:
                self._json(HTTPStatus.ACCEPTED, {"accepted": len(events)})

        def _read_json(self, maximum: int) -> Any:
            media_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if media_type != "application/json":
                raise ValueError("Content-Type 必须为 application/json")
            raw_length = self.headers.get("Content-Length")
            if raw_length is None:
                raise ValueError("缺少 Content-Length")
            try:
                length = int(raw_length)
            except ValueError as exc:
                raise ValueError("Content-Length 无效") from exc
            if length < 0 or length > maximum:
                raise ValueError(f"请求体超过 {maximum} 字节限制")
            data = self.rfile.read(length)
            try:
                return json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("请求体不是有效的 UTF-8 JSON") from exc

        def _json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            LOGGER.info("%s - %s", self.address_string(), format % args)

    return Handler


def _bounded(value: str) -> str:
    return " ".join(value.split())[:500]
