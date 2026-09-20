from dataclasses import replace
import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from agent_notifier.client import daemon_health, notify_daemon
from agent_notifier.config import Config, DefaultsConfig, MonitorConfig
from agent_notifier.daemon import DaemonRuntime
from agent_notifier.notification import normalize_notification
from agent_notifier.sender import SendResult


class FakeSender:
    def __init__(self):
        self.items = []

    def send(self, item):
        self.items.append(item)
        return SendResult("skipped", item.target or "default", (), 0, 0, ())


@pytest.fixture
def running_daemon():
    config = replace(
        Config(),
        defaults=DefaultsConfig("silent", 4),
        monitor=MonitorConfig(listen_port=0, failure_grace_seconds=1),
        groups={"silent": []},
    )
    sender = FakeSender()
    runtime = DaemonRuntime(config, sender=sender)
    port = runtime.server.server_address[1]
    client_config = replace(config, monitor=replace(config.monitor, listen_port=port))
    thread = runtime.start_in_thread()
    yield runtime, client_config, sender
    runtime.close()
    thread.join(2)


def post(config, path, payload, content_type="application/json"):
    data = json.dumps(payload).encode()
    request = Request(
        f"http://127.0.0.1:{config.monitor.listen_port}{path}",
        data=data,
        headers={"Content-Type": content_type},
        method="POST",
    )
    return urlopen(request, timeout=2)


def test_health_and_notify_round_trip(running_daemon) -> None:
    _, config, sender = running_daemon
    assert daemon_health(config)["status"] == "ok"
    notification = normalize_notification(
        config, emoji="✅", title="Done", message="Body", priority=4
    )
    result = notify_daemon(config, notification)
    assert result.status == "skipped"
    assert sender.items == [notification]


def test_otlp_endpoint_accepts_valid_batch_immediately(running_daemon) -> None:
    runtime, config, _ = running_daemon
    payload = {"resourceLogs": [{"scopeLogs": [{"logRecords": []}]}]}
    with post(config, "/v1/logs", payload) as response:
        assert response.status == 202
        assert json.load(response) == {"accepted": 0}
    assert runtime.monitor.pending_count == 0


def test_rejects_wrong_content_type_and_oversize_without_reading_body(running_daemon) -> None:
    _, config, _ = running_daemon
    with pytest.raises(HTTPError) as raised:
        post(config, "/v1/notify", {}, "text/plain")
    assert raised.value.code == 400

    request = Request(
        f"http://127.0.0.1:{config.monitor.listen_port}/v1/notify",
        data=b"{}",
        headers={"Content-Type": "application/json", "Content-Length": str(999999)},
        method="POST",
    )
    with pytest.raises(HTTPError) as raised:
        urlopen(request, timeout=2)
    assert raised.value.code == 400


def test_unknown_endpoint_is_404(running_daemon) -> None:
    _, config, _ = running_daemon
    with pytest.raises(HTTPError) as raised:
        post(config, "/missing", {})
    assert raised.value.code == 404
