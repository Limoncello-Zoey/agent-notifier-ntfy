from dataclasses import replace

import pytest

from agent_notifier.config import Config, DefaultsConfig, MonitorConfig
from agent_notifier.failure_monitor import FailureMonitor, build_notification, classify_failure
from agent_notifier.otlp_receiver import TransportEvent


def config() -> Config:
    return replace(
        Config(),
        defaults=DefaultsConfig("alerts", 4),
        monitor=MonitorConfig(failure_grace_seconds=10, dedupe_window_seconds=30),
        topics={"alerts": "real-alerts"},
    )


def event(**updates) -> TransportEvent:
    values = dict(
        name="codex.api_request",
        session_id="session-1",
        transport="api",
        success=False,
        model="gpt-test",
        status=500,
        attempt=1,
        error="server error",
    )
    values.update(updates)
    return TransportEvent(**values)


@pytest.mark.parametrize(
    "updates, expected",
    [
        ({"status": 401}, "authentication_failed"),
        ({"status": 403}, "authentication_failed"),
        ({"status": 429}, "rate_limited"),
        ({"status": 503}, "server_unavailable"),
        ({"status": None, "error": "TLS handshake failed"}, "connection_failed"),
        ({"status": None, "transport": "sse", "error": "closed"}, "response_stream_disconnected"),
        ({"status": 400}, "transport_failure"),
    ],
)
def test_failure_classification_order(updates, expected) -> None:
    assert classify_failure(event(**updates)) == expected


def test_grace_window_and_recovery_cancellation() -> None:
    sent = []
    monitor = FailureMonitor(config(), lambda item: sent.append(item) or True)
    monitor.ingest(event(), now=0)
    assert monitor.tick(now=9.9) == 0
    monitor.ingest(event(success=True, status=200, error=None), now=10)
    assert monitor.tick(now=20) == 0
    assert sent == []


def test_failure_details_refresh_without_extending_initial_grace() -> None:
    sent = []
    monitor = FailureMonitor(config(), lambda item: sent.append(item) or True)
    monitor.ingest(event(error="first"), now=0)
    monitor.ingest(event(error="second"), now=5)
    # A changed fingerprint is a distinct final failure and starts a new grace window.
    assert monitor.tick(now=10) == 0
    assert monitor.tick(now=15) == 1
    assert "second" in sent[0].message


def test_sessions_are_independent_and_alerts_use_fixed_template() -> None:
    sent = []
    monitor = FailureMonitor(config(), lambda item: sent.append(item) or True)
    monitor.ingest(event(session_id="one", status=429), now=0)
    monitor.ingest(event(session_id="two", status=429), now=0)
    assert monitor.tick(now=10) == 2
    assert [item.title for item in sent] == ["模型服务限流", "模型服务限流"]
    assert all(item.priority == 5 and item.target is None for item in sent)


def test_duplicate_is_suppressed_but_recovery_allows_realert() -> None:
    sent = []
    monitor = FailureMonitor(config(), lambda item: sent.append(item) or True)
    monitor.ingest(event(), now=0)
    assert monitor.tick(now=10) == 1
    monitor.ingest(event(), now=11)
    assert monitor.tick(now=21) == 0
    monitor.ingest(event(success=True, status=200, error=None), now=22)
    monitor.ingest(event(), now=23)
    assert monitor.tick(now=33) == 1
    assert len(sent) == 2


def test_full_send_queue_keeps_due_alert_for_retry() -> None:
    accept = False
    sent = []

    def enqueue(item):
        sent.append(item)
        return accept

    monitor = FailureMonitor(config(), enqueue)
    monitor.ingest(event(), now=0)
    assert monitor.tick(now=10) == 0
    assert monitor.pending_count == 1
    accept = True
    assert monitor.tick(now=11) == 1
    assert monitor.pending_count == 0


def test_notification_redacts_and_bounds_secrets() -> None:
    item = build_notification(
        config(),
        event(status=None, error="Authorization: Bearer secret-value token=abcd " + "x" * 400),
        "connection_failed",
    )
    assert "secret-value" not in item.message
    assert "abcd" not in item.message
    error_line = item.message.split("错误：", 1)[1]
    assert len(error_line) <= 240
