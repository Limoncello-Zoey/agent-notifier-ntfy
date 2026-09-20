from pathlib import Path

from agent_notifier.config import parse_config, save_config
from agent_notifier.diagnostics import RULES_END, RULES_START, doctor_checks


def test_doctor_covers_complete_deployment_state(tmp_path: Path, monkeypatch) -> None:
    app_config = tmp_path / "app" / "config.toml"
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    save_config(parse_config({}), app_config)
    (codex_home / "config.toml").write_text(
        """
[otel]
log_user_prompt = false
exporter = { otlp-http = { endpoint = "http://127.0.0.1:4318/v1/logs", protocol = "json" } }

[mcp_servers.agent-notifier]
command = "/stable/bin/agent-notifier"
args = ["mcp"]
enabled = true
enabled_tools = ["ntfy_send"]

[mcp_servers.agent-notifier.tools.ntfy_send]
approval_mode = "approve"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    (codex_home / "AGENTS.md").write_text(
        f"other instructions\n{RULES_START}\n调用 ntfy_send 并选择优先级。\n{RULES_END}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("agent_notifier.diagnostics.shutil.which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(
        "agent_notifier.diagnostics.subprocess.run",
        lambda *args, **kwargs: type("Completed", (), {"returncode": 0, "stdout": "active\n", "stderr": ""})(),
    )
    monkeypatch.setattr("agent_notifier.diagnostics.daemon_health", lambda config: {"status": "ok"})
    checks = doctor_checks(
        {
            "AGENT_NOTIFIER_CONFIG": str(app_config),
            "CODEX_HOME": str(codex_home),
            "HOME": str(tmp_path),
        }
    )
    assert [item["name"] for item in checks] == [
        "cli", "config", "ntfy", "daemon", "mcp", "otel", "notification_rules"
    ]
    assert all(item["ok"] for item in checks)


def test_override_file_is_the_effective_global_instruction_file(tmp_path: Path, monkeypatch) -> None:
    app_config = tmp_path / "config.toml"
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    save_config(parse_config({}), app_config)
    (codex_home / "config.toml").write_text("", encoding="utf-8")
    (codex_home / "AGENTS.md").write_text(
        f"{RULES_START}\n调用 ntfy_send 并设置优先级。\n{RULES_END}\n", encoding="utf-8"
    )
    (codex_home / "AGENTS.override.md").write_text("override without rules\n", encoding="utf-8")
    monkeypatch.setattr("agent_notifier.diagnostics.shutil.which", lambda name: None)
    checks = doctor_checks(
        {"AGENT_NOTIFIER_CONFIG": str(app_config), "CODEX_HOME": str(codex_home)}
    )
    rules = next(item for item in checks if item["name"] == "notification_rules")
    assert rules["ok"] is False
    assert "AGENTS.override.md" in rules["detail"]
