from pathlib import Path

from agent_notifier.config import parse_config, save_config
from agent_notifier.diagnostics import RULES_END, RULES_START, _project_root, doctor_checks


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
        f"other instructions\n{RULES_START}\n"
        "调用 ntfy_send 并选择优先级；长程 goal 每完成一个小点通知，"
        f"每次 git commit 成功后通知。\n{RULES_END}\n",
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


def test_doctor_rejects_legacy_notification_rules(tmp_path: Path, monkeypatch) -> None:
    app_config = tmp_path / "config.toml"
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    save_config(parse_config({}), app_config)
    (codex_home / "config.toml").write_text("", encoding="utf-8")
    (codex_home / "AGENTS.md").write_text(
        "规则块外提到长程小点、goal 和 git commit。\n"
        f"{RULES_START}\n调用 ntfy_send 并设置优先级。\n{RULES_END}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("agent_notifier.diagnostics.shutil.which", lambda name: None)

    checks = doctor_checks(
        {"AGENT_NOTIFIER_CONFIG": str(app_config), "CODEX_HOME": str(codex_home)}
    )

    rules = next(item for item in checks if item["name"] == "notification_rules")
    assert rules["ok"] is False
    assert "长程小点" in rules["detail"]
    assert "git commit" in rules["detail"]


def test_doctor_supports_isolated_project_scope(tmp_path: Path, monkeypatch) -> None:
    app_config = tmp_path / "app" / "config.toml"
    codex_home = tmp_path / "codex"
    project = tmp_path / "project"
    codex_home.mkdir()
    (project / ".git").mkdir(parents=True)
    (project / ".codex").mkdir()
    save_config(parse_config({}), app_config)
    (project / ".codex" / "config.toml").write_text(
        """
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
    (project / "AGENTS.md").write_text(
        f"{RULES_START}\n调用 ntfy_send 并选择优先级；长程 goal 每完成一个小点通知，"
        f"每次 git commit 成功后通知。\n{RULES_END}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("agent_notifier.diagnostics.shutil.which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(
        "agent_notifier.diagnostics.subprocess.run",
        lambda *args, **kwargs: type(
            "Completed", (), {"returncode": 0, "stdout": "active\n", "stderr": ""}
        )(),
    )
    monkeypatch.setattr("agent_notifier.diagnostics.daemon_health", lambda config: {"status": "ok"})

    checks = doctor_checks(
        {
            "AGENT_NOTIFIER_CONFIG": str(app_config),
            "CODEX_HOME": str(codex_home),
            "HOME": str(tmp_path),
        },
        scope="project",
        project_root=project,
    )

    assert [item["name"] for item in checks] == [
        "cli",
        "config",
        "ntfy",
        "daemon",
        "project_root",
        "mcp",
        "otel",
        "notification_rules",
        "scope_isolation",
    ]
    assert all(item["ok"] for item in checks)


def test_project_scope_rejects_local_otel_and_global_activation(
    tmp_path: Path, monkeypatch
) -> None:
    app_config = tmp_path / "app" / "config.toml"
    codex_home = tmp_path / "codex"
    project = tmp_path / "project"
    codex_home.mkdir()
    (project / ".git").mkdir(parents=True)
    (project / ".codex").mkdir()
    save_config(parse_config({}), app_config)
    (project / ".codex" / "config.toml").write_text(
        """
[otel]
log_user_prompt = false

[mcp_servers.agent-notifier]
command = "agent-notifier"
args = ["mcp"]
enabled_tools = ["ntfy_send"]

[mcp_servers.agent-notifier.tools.ntfy_send]
approval_mode = "approve"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    (project / "AGENTS.md").write_text(
        f"{RULES_START}\nntfy_send 优先级 小点 goal git commit\n{RULES_END}\n",
        encoding="utf-8",
    )
    (codex_home / "config.toml").write_text(
        """
[otel]
exporter = { otlp-http = { endpoint = "http://127.0.0.1:4318/v1/logs", protocol = "json" } }

[mcp_servers.agent-notifier]
command = "agent-notifier"
args = ["mcp"]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    (codex_home / "AGENTS.md").write_text(
        f"{RULES_START}\nglobal rules\n{RULES_END}\n", encoding="utf-8"
    )
    monkeypatch.setattr("agent_notifier.diagnostics.shutil.which", lambda name: None)

    checks = doctor_checks(
        {
            "AGENT_NOTIFIER_CONFIG": str(app_config),
            "CODEX_HOME": str(codex_home),
        },
        scope="project",
        project_root=project,
    )

    otel = next(item for item in checks if item["name"] == "otel")
    isolation = next(item for item in checks if item["name"] == "scope_isolation")
    assert otel["ok"] is False
    assert "会忽略" in otel["detail"]
    assert isolation["ok"] is False
    assert isolation["detail"] == [
        "用户级 MCP 仍包含 agent-notifier",
        "用户级 OTel 仍指向 agent-notifierd",
        f"全局 Agent 指令仍包含 Agent Notifier 规则块: {codex_home / 'AGENTS.md'}",
    ]


def test_project_root_is_discovered_from_nested_working_directory(
    tmp_path: Path, monkeypatch
) -> None:
    project = tmp_path / "project"
    nested = project / "src" / "package"
    (project / ".git").mkdir(parents=True)
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)

    assert _project_root(None) == (project, True)


def test_explicit_project_root_is_not_replaced_by_parent_repository(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    requested = tmp_path / "not-a-project"
    requested.mkdir()

    assert _project_root(requested) == (requested, False)
