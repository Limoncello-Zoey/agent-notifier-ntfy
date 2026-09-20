"""Read-only deployment diagnostics used by ``agent-notifier doctor``."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
from typing import Any, Mapping

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib

from .client import daemon_health
from .config import Config, config_path, load_config
from .errors import AgentNotifierError


RULES_START = "<!-- agent-notifier:rules:start -->"
RULES_END = "<!-- agent-notifier:rules:end -->"


def doctor_checks(
    environ: Mapping[str, str] | None = None,
    *,
    scope: str = "global",
    project_root: Path | None = None,
) -> list[dict[str, Any]]:
    if scope not in {"global", "project"}:
        raise ValueError(f"未知诊断作用域: {scope}")
    env = os.environ if environ is None else environ
    checks: list[dict[str, Any]] = []

    executable = shutil.which("agent-notifier")
    checks.append(_check("cli", executable is not None, executable or "PATH 中未找到 agent-notifier"))

    path = config_path(env)
    try:
        config = load_config(path)
    except AgentNotifierError as exc:
        config = None
        checks.append(_check("config", False, str(exc)))
    else:
        checks.append(_check("config", True, str(path)))

    ntfy = shutil.which("ntfy")
    checks.append(_check("ntfy", ntfy is not None, ntfy or "PATH 中未找到 ntfy"))
    checks.append(_daemon_check(config))

    codex_home = _codex_home(env)
    if scope == "project":
        root, root_ok = _project_root(project_root)
        checks.append(
            _check(
                "project_root",
                root_ok,
                str(root) if root_ok else f"未找到 Git 项目根目录: {root}",
            )
        )
        codex_config_path = root / ".codex" / "config.toml"
        rules_root = root
    else:
        codex_config_path = codex_home / "config.toml"
        rules_root = codex_home
    try:
        codex_config = _load_toml(codex_config_path)
    except ValueError as exc:
        checks.append(_check("mcp", False, str(exc)))
        checks.append(_check("otel", False, str(exc)))
    else:
        checks.append(_mcp_check(codex_config, codex_config_path))
        if scope == "project":
            checks.append(_project_otel_check(codex_config, codex_config_path))
        else:
            checks.append(_otel_check(codex_config, codex_config_path, config))
    checks.append(_rules_check(rules_root, scope=scope))
    if scope == "project":
        checks.append(_project_isolation_check(codex_home, config))
    return checks


def _daemon_check(config: Config | None) -> dict[str, Any]:
    if config is None:
        return _check("daemon", False, "应用配置无效，无法执行健康检查")
    service_ok = False
    systemctl = shutil.which("systemctl")
    service_detail = "PATH 中未找到 systemctl"
    if systemctl:
        try:
            completed = subprocess.run(
                [systemctl, "--user", "is-active", "agent-notifier.service"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            service_detail = str(exc)
        else:
            service_detail = " ".join((completed.stdout or completed.stderr).split())[:200]
            service_ok = completed.returncode == 0 and service_detail == "active"
    try:
        health = daemon_health(config)
    except AgentNotifierError as exc:
        health_ok = False
        health_detail: object = str(exc)
    else:
        health_ok = health.get("status") == "ok"
        health_detail = health
    return _check(
        "daemon",
        service_ok and health_ok,
        {"systemd": service_detail, "health": health_detail},
    )


def _mcp_check(raw: Mapping[str, Any], path: Path) -> dict[str, Any]:
    servers = raw.get("mcp_servers")
    entry = servers.get("agent-notifier") if isinstance(servers, dict) else None
    if not isinstance(entry, dict):
        return _check("mcp", False, f"{path} 中缺少 mcp_servers.agent-notifier")
    command = entry.get("command")
    command_ok = isinstance(command, str) and Path(command).name == "agent-notifier"
    args_ok = entry.get("args") == ["mcp"]
    enabled_ok = entry.get("enabled", True) is not False
    tools_ok = entry.get("enabled_tools") == ["ntfy_send"]
    tools = entry.get("tools")
    tool = tools.get("ntfy_send") if isinstance(tools, dict) else None
    approval_ok = isinstance(tool, dict) and tool.get("approval_mode") == "approve"
    ok = command_ok and args_ok and enabled_ok and tools_ok and approval_ok
    detail = str(path) if ok else "MCP 必须启用 agent-notifier mcp，且仅允许自动批准 ntfy_send"
    return _check("mcp", ok, detail)


def _otel_check(raw: Mapping[str, Any], path: Path, config: Config | None) -> dict[str, Any]:
    otel = raw.get("otel")
    exporter = otel.get("exporter") if isinstance(otel, dict) else None
    http = exporter.get("otlp-http") if isinstance(exporter, dict) else None
    host = config.monitor.listen_host if config is not None else "127.0.0.1"
    port = config.monitor.listen_port if config is not None else 4318
    rendered_host = f"[{host}]" if ":" in host else host
    expected_endpoint = f"http://{rendered_host}:{port}/v1/logs"
    ok = (
        isinstance(otel, dict)
        and otel.get("log_user_prompt") is False
        and isinstance(http, dict)
        and http.get("endpoint") == expected_endpoint
        and http.get("protocol") == "json"
    )
    detail = str(path) if ok else "OTel 必须使用本地 OTLP/HTTP JSON，且 log_user_prompt=false"
    return _check("otel", ok, detail)


def _project_otel_check(raw: Mapping[str, Any], path: Path) -> dict[str, Any]:
    ok = "otel" not in raw
    detail = (
        "项目限定模式已禁用 OTel；仅启用 Agent/MCP 通知"
        if ok
        else f"{path} 不得包含 otel；Codex 会忽略项目级遥测配置"
    )
    return _check("otel", ok, detail)


def _rules_check(root: Path, *, scope: str = "global") -> dict[str, Any]:
    path = _effective_rules_path(root)
    if not path.is_file():
        label = "项目" if scope == "project" else "全局"
        return _check("notification_rules", False, f"{label} Agent 指令不存在: {path}")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return _check("notification_rules", False, f"无法读取 {path}: {exc}")
    markers_ok = text.count(RULES_START) == 1 and text.count(RULES_END) == 1
    rules_text = ""
    if markers_ok:
        start = text.index(RULES_START) + len(RULES_START)
        end = text.index(RULES_END)
        if start <= end:
            rules_text = text[start:end]
        else:
            markers_ok = False
    required_semantics = ("ntfy_send", "优先级", "小点", "goal", "git commit")
    semantics_ok = all(term in rules_text for term in required_semantics)
    ok = markers_ok and semantics_ok
    detail = (
        str(path)
        if ok
        else f"{path} 中缺少唯一且完整的 Agent Notifier 规则块"
        "（需覆盖交还、长程小点、goal 和 git commit）"
    )
    return _check("notification_rules", ok, detail)


def _project_isolation_check(codex_home: Path, config: Config | None) -> dict[str, Any]:
    conflicts: list[str] = []
    user_config_path = codex_home / "config.toml"
    if user_config_path.is_file():
        try:
            user_config = _load_toml(user_config_path)
        except ValueError as exc:
            return _check("scope_isolation", False, str(exc))
        servers = user_config.get("mcp_servers")
        if isinstance(servers, dict) and isinstance(servers.get("agent-notifier"), dict):
            conflicts.append("用户级 MCP 仍包含 agent-notifier")
        if _otel_targets_notifier(user_config, config):
            conflicts.append("用户级 OTel 仍指向 agent-notifierd")

    for rules_path in (codex_home / "AGENTS.override.md", codex_home / "AGENTS.md"):
        if not rules_path.is_file():
            continue
        try:
            rules_text = rules_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            return _check("scope_isolation", False, f"无法读取 {rules_path}: {exc}")
        if RULES_START in rules_text or RULES_END in rules_text:
            conflicts.append(f"全局 Agent 指令仍包含 Agent Notifier 规则块: {rules_path}")

    ok = not conflicts
    detail: object = (
        "Agent Notifier 的 MCP、OTel 和通知规则均未在用户级激活"
        if ok
        else conflicts
    )
    return _check("scope_isolation", ok, detail)


def _otel_targets_notifier(raw: Mapping[str, Any], config: Config | None) -> bool:
    otel = raw.get("otel")
    exporter = otel.get("exporter") if isinstance(otel, dict) else None
    http = exporter.get("otlp-http") if isinstance(exporter, dict) else None
    endpoint = http.get("endpoint") if isinstance(http, dict) else None
    if not isinstance(endpoint, str):
        return False
    host = config.monitor.listen_host if config is not None else "127.0.0.1"
    port = config.monitor.listen_port if config is not None else 4318
    rendered_host = f"[{host}]" if ":" in host else host
    return endpoint == f"http://{rendered_host}:{port}/v1/logs"


def _effective_rules_path(root: Path) -> Path:
    override = root / "AGENTS.override.md"
    regular = root / "AGENTS.md"
    return override if override.is_file() and override.stat().st_size > 0 else regular


def _project_root(value: Path | None) -> tuple[Path, bool]:
    candidate = (value or Path.cwd()).expanduser().absolute()
    if candidate.is_file():
        candidate = candidate.parent
    if value is not None:
        return candidate, (candidate / ".git").exists()
    for current in (candidate, *candidate.parents):
        if (current / ".git").exists():
            return current, True
    return candidate, False


def _codex_home(env: Mapping[str, str]) -> Path:
    value = env.get("CODEX_HOME")
    if value:
        return Path(value).expanduser().absolute()
    home = Path(env.get("HOME", str(Path.home()))).expanduser()
    return (home / ".codex").absolute()


def _load_toml(path: Path) -> Mapping[str, Any]:
    try:
        with path.open("rb") as stream:
            raw = tomllib.load(stream)
    except FileNotFoundError as exc:
        raise ValueError(f"Codex 配置不存在: {path}") from exc
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"Codex 配置无法读取: {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"Codex 配置根节点无效: {path}")
    return raw


def _check(name: str, ok: bool, detail: object) -> dict[str, Any]:
    return {"name": name, "ok": ok, "detail": detail}
