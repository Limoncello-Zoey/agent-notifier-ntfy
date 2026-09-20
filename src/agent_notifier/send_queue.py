"""Bounded FIFO queue with one worker for globally ordered ntfy publishing."""

from __future__ import annotations

from dataclasses import dataclass, field
from queue import Full, Queue
import threading
from typing import Callable

from .errors import DaemonError
from .notification import Notification
from .sender import SendResult


class QueueFullError(DaemonError):
    """The daemon cannot accept more pending notifications."""


class QueueTimeoutError(DaemonError):
    """A queued notification did not finish before the caller deadline."""


@dataclass(slots=True)
class _Task:
    notification: Notification
    done: threading.Event = field(default_factory=threading.Event)
    result: SendResult | None = None
    error: BaseException | None = None


class SendQueue:
    def __init__(self, sender: Callable[[Notification], SendResult], capacity: int = 100) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self._sender = sender
        self._queue: Queue[_Task | None] = Queue(maxsize=capacity)
        self._thread = threading.Thread(
            target=self._work, name="agent-notifier-sender", daemon=True
        )
        self._started = False
        self._closed = False
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self._closed:
                raise DaemonError("发送队列已关闭")
            if not self._started:
                self._thread.start()
                self._started = True

    def submit(self, notification: Notification, timeout: float) -> SendResult:
        self.start()
        task = _Task(notification)
        try:
            self._queue.put_nowait(task)
        except Full as exc:
            raise QueueFullError("发送队列已满，请稍后重试") from exc
        if not task.done.wait(timeout):
            raise QueueTimeoutError(f"等待发送结果超过 {timeout:g} 秒")
        if task.error is not None:
            if isinstance(task.error, Exception):
                raise task.error
            raise DaemonError("发送 Worker 异常终止") from task.error
        assert task.result is not None
        return task.result

    def submit_background(self, notification: Notification) -> bool:
        """Enqueue a monitor notification without waiting for network delivery."""
        self.start()
        try:
            self._queue.put_nowait(_Task(notification))
        except Full:
            return False
        return True

    def shutdown(self, timeout: float = 5.0) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            started = self._started
        if started:
            self._queue.put(None)
            self._thread.join(timeout)

    @property
    def pending(self) -> int:
        return self._queue.qsize()

    def _work(self) -> None:
        while True:
            task = self._queue.get()
            try:
                if task is None:
                    return
                try:
                    task.result = self._sender(task.notification)
                except BaseException as exc:  # preserve worker availability after one bad task
                    task.error = exc
                finally:
                    task.done.set()
            finally:
                self._queue.task_done()
