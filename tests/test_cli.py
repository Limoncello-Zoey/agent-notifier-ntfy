import json
from pathlib import Path

import pytest

from agent_notifier.cli import main
from agent_notifier.config import load_config, parse_config, save_config
from agent_notifier.sender import SendResult


@pytest.fixture
def config_file(tmp_path: Path, monkeypatch):
    path = tmp_path / "config.toml"
    monkeypatch.setenv("AGENT_NOTIFIER_CONFIG", str(path))
    return path


def test_config_lifecycle(config_file, capsys) -> None:
    assert main(["config", "path"]) == 0
    assert str(config_file) in capsys.readouterr().out
    assert main(["config", "init"]) == 0
    assert main(["config", "validate"]) == 0
    assert main(["config", "init"]) == 1
    assert "拒绝覆盖" in capsys.readouterr().err


def test_topic_group_and_default_commands_are_persisted(config_file, capsys) -> None:
    save_config(parse_config({}), config_file)
    assert main(["topic", "set", "a", "real-a"]) == 0
    assert main(["topic", "set", "b", "real-b"]) == 0
    assert main(["group", "set", "all", "a", "b"]) == 0
    assert main(["default", "set", "all"]) == 0
    assert main(["group", "resolve", "all"]) == 0
    output = capsys.readouterr().out
    assert "real-a\nreal-b" in output
    config = load_config(config_file)
    assert config.defaults.target == "all"
    assert config.groups["all"] == ["a", "b"]


def test_invalid_mutation_does_not_modify_file(config_file, capsys) -> None:
    save_config(parse_config({"topics": {"a": "real-a"}, "groups": {"all": ["a"]}}), config_file)
    before = config_file.read_bytes()
    assert main(["topic", "remove", "a"]) == 1
    assert config_file.read_bytes() == before
    assert "不存在" in capsys.readouterr().err


def test_group_set_without_members_creates_empty_group(config_file) -> None:
    save_config(parse_config({}), config_file)
    assert main(["group", "set", "silent"]) == 0
    assert load_config(config_file).groups == {"silent": []}


def test_send_json_uses_daemon_client(config_file, monkeypatch, capsys) -> None:
    save_config(parse_config({"topics": {"a": "real-a"}, "defaults": {"target": "a"}}), config_file)
    observed = []

    def notify(config, item):
        observed.append(item)
        return SendResult("success", "a", ("real-a",), 1, 0, ())

    monkeypatch.setattr("agent_notifier.cli.notify_daemon", notify)
    assert main(["send", "--message", "Done", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "success"
    assert observed[0].emoji == "ℹ️"
    assert observed[0].title == "Agent Notifier"


def test_doctor_json_reports_failures_without_crashing(config_file, monkeypatch, capsys) -> None:
    monkeypatch.setattr("agent_notifier.diagnostics.shutil.which", lambda name: None)
    assert main(["doctor", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert {item["name"] for item in payload["checks"]} == {
        "cli", "config", "ntfy", "daemon", "mcp", "otel", "notification_rules"
    }
