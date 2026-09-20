import threading

import pytest

from agent_notifier.config import Config
from agent_notifier.notification import normalize_notification
from agent_notifier.send_queue import QueueFullError, QueueTimeoutError, SendQueue
from agent_notifier.sender import SendResult


def note(message: str):
    return normalize_notification(Config(), emoji="i", title="t", message=message, priority=4)


def success(item):
    return SendResult("skipped", item.target or "empty", (), 0, 0, ())


def test_single_worker_preserves_fifo_order() -> None:
    received = []
    first_started = threading.Event()
    release = threading.Event()

    def sender(item):
        received.append(item.message)
        if item.message == "one":
            first_started.set()
            release.wait(1)
        return success(item)

    queue = SendQueue(sender, capacity=3)
    results = []
    one = threading.Thread(target=lambda: results.append(queue.submit(note("one"), 2)))
    two = threading.Thread(target=lambda: results.append(queue.submit(note("two"), 2)))
    one.start()
    assert first_started.wait(1)
    two.start()
    release.set()
    one.join()
    two.join()
    queue.shutdown()
    assert received == ["one", "two"]
    assert len(results) == 2


def test_sender_exception_is_returned_and_worker_continues() -> None:
    calls = []

    def sender(item):
        calls.append(item.message)
        if item.message == "bad":
            raise RuntimeError("boom")
        return success(item)

    queue = SendQueue(sender)
    with pytest.raises(RuntimeError, match="boom"):
        queue.submit(note("bad"), 1)
    assert queue.submit(note("good"), 1).status == "skipped"
    queue.shutdown()
    assert calls == ["bad", "good"]


def test_background_completion_reports_sender_result() -> None:
    completed = threading.Event()
    outcomes = []
    queue = SendQueue(success)

    assert queue.submit_background(
        note("background"),
        lambda result, error: (outcomes.append((result, error)), completed.set()),
    )
    assert completed.wait(1)
    queue.shutdown()

    assert outcomes[0][0].status == "skipped"
    assert outcomes[0][1] is None


def test_background_completion_reports_sender_exception() -> None:
    completed = threading.Event()
    outcomes = []

    def fail(item):
        raise RuntimeError("boom")

    queue = SendQueue(fail)
    assert queue.submit_background(
        note("background"),
        lambda result, error: (outcomes.append((result, error)), completed.set()),
    )
    assert completed.wait(1)
    queue.shutdown()

    assert outcomes[0][0] is None
    assert isinstance(outcomes[0][1], RuntimeError)


def test_wait_timeout_is_explicit() -> None:
    release = threading.Event()

    def sender(item):
        release.wait(1)
        return success(item)

    queue = SendQueue(sender)
    try:
        with pytest.raises(QueueTimeoutError, match="超过"):
            queue.submit(note("slow"), 0.01)
    finally:
        release.set()
        queue.shutdown()


def test_full_queue_rejects_without_unbounded_growth() -> None:
    started = threading.Event()
    release = threading.Event()

    def sender(item):
        started.set()
        release.wait(1)
        return success(item)

    queue = SendQueue(sender, capacity=1)
    first = threading.Thread(target=lambda: queue.submit(note("one"), 2))
    first.start()
    assert started.wait(1)
    assert queue.submit_background(note("two"))
    with pytest.raises(QueueFullError, match="队列已满"):
        queue.submit(note("three"), 0.1)
    release.set()
    first.join()
    queue.shutdown()


def test_shutdown_does_not_block_when_queue_is_full() -> None:
    started = threading.Event()
    release = threading.Event()

    def sender(item):
        started.set()
        release.wait(1)
        return success(item)

    queue = SendQueue(sender, capacity=1)
    queue.start()
    assert queue.submit_background(note("one"))
    assert started.wait(1)
    assert queue.submit_background(note("two"))
    queue.shutdown(timeout=0.01)
    release.set()
