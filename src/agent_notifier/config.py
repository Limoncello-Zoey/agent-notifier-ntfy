"""Strict TOML configuration loading and atomic persistence."""

from __future__ import annotations

from dataclasses import dataclass, field
import ipaddress
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Mapping
from urllib.parse import urlsplit

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised only on Python 3.10
    import tomli as tomllib

from .errors import ConfigError


CONFIG_ENV = "AGENT_NOTIFIER_CONFIG"
NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
ROOT_KEYS = {"version", "server", "defaults", "monitor", "topics", "groups"}
SECTION_KEYS = {
    "server": {"base_url"},
    "defaults": {"target", "priority"},
    "monitor": {
        "listen_host",
        "listen_port",
        "failure_grace_seconds",
        "dedupe_window_seconds",
        "queue_capacity",
        "request_timeout_seconds",
    },
}


@dataclass(frozen=True, slots=True)
class ServerConfig:
    base_url: str = "https://ntfy.sh"


@dataclass(frozen=True, slots=True)
class DefaultsConfig:
    target: str | None = None
    priority: int = 4


@dataclass(frozen=True, slots=True)
class MonitorConfig:
    listen_host: str = "127.0.0.1"
    listen_port: int = 4318
    failure_grace_seconds: int = 30
    dedupe_window_seconds: int = 300
    queue_capacity: int = 100
    request_timeout_seconds: int = 60


@dataclass(frozen=True, slots=True)
class Config:
    version: int = 1
    server: ServerConfig = field(default_factory=ServerConfig)
    defaults: DefaultsConfig = field(default_factory=DefaultsConfig)
    monitor: MonitorConfig = field(default_factory=MonitorConfig)
    topics: dict[str, str] = field(default_factory=dict)
    groups: dict[str, list[str]] = field(default_factory=dict)


def config_path(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    override = env.get(CONFIG_ENV)
    if override:
        path = Path(override).expanduser()
        if not path.is_absolute():
            raise ConfigError(f"{CONFIG_ENV} 必须是绝对路径: {override}")
        return path
    base = Path(env.get("XDG_CONFIG_HOME", Path.home() / ".config")).expanduser()
    return (base / "agent-notifier" / "config.toml").absolute()


def load_config(path: Path | None = None) -> Config:
    resolved = path or config_path()
    try:
        with resolved.open("rb") as stream:
            raw = tomllib.load(stream)
    except FileNotFoundError as exc:
        raise ConfigError(
            f"配置文件不存在: {resolved}；请运行 agent-notifier config init"
        ) from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"配置文件 TOML 无效: {exc}") from exc
    return parse_config(raw)


def parse_config(raw: Mapping[str, Any]) -> Config:
    _reject_unknown("根配置", raw, ROOT_KEYS)
    version = raw.get("version", 1)
    if type(version) is not int or version != 1:
        raise ConfigError("version 必须为整数 1")

    server_raw = _section(raw, "server")
    defaults_raw = _section(raw, "defaults")
    monitor_raw = _section(raw, "monitor")
    _reject_unknown("server", server_raw, SECTION_KEYS["server"])
    _reject_unknown("defaults", defaults_raw, SECTION_KEYS["defaults"])
    _reject_unknown("monitor", monitor_raw, SECTION_KEYS["monitor"])

    base_url = server_raw.get("base_url", "https://ntfy.sh")
    if not isinstance(base_url, str) or not base_url.strip():
        raise ConfigError("server.base_url 必须是非空字符串")
    base_url = base_url.strip().rstrip("/")
    if not (base_url.startswith("https://") or base_url.startswith("http://")):
        raise ConfigError("server.base_url 必须使用 http:// 或 https://")
    parsed_url = urlsplit(base_url)
    if not parsed_url.hostname:
        raise ConfigError("server.base_url 必须包含主机名")
    if parsed_url.username is not None or parsed_url.password is not None:
        raise ConfigError("server.base_url 第一版不支持内嵌用户名或密码")
    if parsed_url.query or parsed_url.fragment:
        raise ConfigError("server.base_url 不能包含查询参数或片段")

    target = defaults_raw.get("target")
    if target is not None:
        _validate_name(target, "defaults.target")
    priority = defaults_raw.get("priority", 4)
    if type(priority) is not int or priority not in (4, 5):
        raise ConfigError("defaults.priority 必须为 4 或 5")

    listen_host = monitor_raw.get("listen_host", "127.0.0.1")
    if not isinstance(listen_host, str):
        raise ConfigError("monitor.listen_host 必须是回环 IP 地址")
    try:
        if not ipaddress.ip_address(listen_host).is_loopback:
            raise ValueError
    except ValueError as exc:
        raise ConfigError("monitor.listen_host 第一版只允许回环 IP 地址") from exc

    listen_port = _positive_int(monitor_raw, "listen_port", 4318)
    if listen_port > 65535:
        raise ConfigError("monitor.listen_port 必须在 1..65535 范围内")
    grace = _positive_int(monitor_raw, "failure_grace_seconds", 30)
    dedupe = _positive_int(monitor_raw, "dedupe_window_seconds", 300)
    capacity = _positive_int(monitor_raw, "queue_capacity", 100)
    timeout = _positive_int(monitor_raw, "request_timeout_seconds", 60)

    topics_raw = _section(raw, "topics")
    groups_raw = _section(raw, "groups")
    topics: dict[str, str] = {}
    groups: dict[str, list[str]] = {}
    for name, topic in topics_raw.items():
        _validate_name(name, "topics 名称")
        if not isinstance(topic, str) or not topic.strip():
            raise ConfigError(f"topics.{name} 必须是非空字符串")
        topics[name] = topic.strip()
    for name, members in groups_raw.items():
        _validate_name(name, "groups 名称")
        if not isinstance(members, list) or any(not isinstance(item, str) for item in members):
            raise ConfigError(f"groups.{name} 必须是字符串数组")
        for member in members:
            _validate_name(member, f"groups.{name} 成员")
        groups[name] = list(members)

    overlap = sorted(set(topics) & set(groups))
    if overlap:
        raise ConfigError(f"话题与话题组重名: {', '.join(overlap)}")
    names = set(topics) | set(groups)
    if target is not None and target not in names:
        raise ConfigError(f"defaults.target 引用了不存在的目标: {target}")
    for group, members in groups.items():
        missing = [member for member in members if member not in names]
        if missing:
            raise ConfigError(f"groups.{group} 引用了不存在的目标: {', '.join(missing)}")
    _validate_cycles(groups)

    return Config(
        version=version,
        server=ServerConfig(base_url),
        defaults=DefaultsConfig(target, priority),
        monitor=MonitorConfig(listen_host, listen_port, grace, dedupe, capacity, timeout),
        topics=topics,
        groups=groups,
    )


def save_config(config: Config, path: Path | None = None) -> Path:
    destination = path or config_path()
    destination.parent.mkdir(parents=True, exist_ok=True)
    data = render_config(config)
    fd, temporary = tempfile.mkstemp(prefix=".config.toml.", dir=destination.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    return destination


def validate_config(config: Config) -> Config:
    """Revalidate a programmatically modified immutable Config instance."""
    return parse_config(
        {
            "version": config.version,
            "server": {"base_url": config.server.base_url},
            "defaults": {
                **({"target": config.defaults.target} if config.defaults.target is not None else {}),
                "priority": config.defaults.priority,
            },
            "monitor": {
                "listen_host": config.monitor.listen_host,
                "listen_port": config.monitor.listen_port,
                "failure_grace_seconds": config.monitor.failure_grace_seconds,
                "dedupe_window_seconds": config.monitor.dedupe_window_seconds,
                "queue_capacity": config.monitor.queue_capacity,
                "request_timeout_seconds": config.monitor.request_timeout_seconds,
            },
            "topics": config.topics,
            "groups": config.groups,
        }
    )


def render_config(config: Config) -> str:
    lines = [
        "version = 1",
        "",
        "[server]",
        f"base_url = {_toml_string(config.server.base_url)}",
        "",
        "[defaults]",
    ]
    if config.defaults.target is not None:
        lines.append(f"target = {_toml_string(config.defaults.target)}")
    lines.extend(
        [
            f"priority = {config.defaults.priority}",
            "",
            "[monitor]",
            f"listen_host = {_toml_string(config.monitor.listen_host)}",
            f"listen_port = {config.monitor.listen_port}",
            f"failure_grace_seconds = {config.monitor.failure_grace_seconds}",
            f"dedupe_window_seconds = {config.monitor.dedupe_window_seconds}",
            f"queue_capacity = {config.monitor.queue_capacity}",
            f"request_timeout_seconds = {config.monitor.request_timeout_seconds}",
            "",
            "[topics]",
        ]
    )
    lines.extend(f"{_toml_key(k)} = {_toml_string(v)}" for k, v in config.topics.items())
    lines.extend(["", "[groups]"])
    for key, values in config.groups.items():
        rendered = ", ".join(_toml_string(value) for value in values)
        lines.append(f"{_toml_key(key)} = [{rendered}]")
    return "\n".join(lines) + "\n"


def initial_config_text() -> str:
    return """# Agent Notifier 配置。添加话题后可用 default set 设置默认目标。
version = 1

[server]
base_url = "https://ntfy.sh"

[defaults]
priority = 4

[monitor]
listen_host = "127.0.0.1"
listen_port = 4318
failure_grace_seconds = 30
dedupe_window_seconds = 300
queue_capacity = 100
request_timeout_seconds = 60

[topics]

[groups]
"""


def init_config(path: Path | None = None) -> Path:
    destination = path or config_path()
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with destination.open("x", encoding="utf-8") as stream:
            stream.write(initial_config_text())
    except FileExistsError as exc:
        raise ConfigError(f"配置文件已存在，拒绝覆盖: {destination}") from exc
    return destination


def _section(raw: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = raw.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"{name} 必须是 TOML 表")
    return value


def _reject_unknown(label: str, raw: Mapping[str, Any], allowed: set[str]) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ConfigError(f"{label} 包含未知字段: {', '.join(unknown)}")


def _validate_name(value: object, label: str) -> None:
    if not isinstance(value, str) or not NAME_RE.fullmatch(value):
        raise ConfigError(f"{label} 必须由字母、数字、点、下划线或短横线组成")


def _positive_int(raw: Mapping[str, Any], key: str, default: int) -> int:
    value = raw.get(key, default)
    if type(value) is not int or value <= 0:
        raise ConfigError(f"monitor.{key} 必须是正整数")
    return value


def _validate_cycles(groups: Mapping[str, list[str]]) -> None:
    visited: set[str] = set()
    active: list[str] = []

    def visit(name: str) -> None:
        if name in active:
            start = active.index(name)
            cycle = active[start:] + [name]
            raise ConfigError(f"话题组存在循环引用: {' -> '.join(cycle)}")
        if name in visited:
            return
        active.append(name)
        for member in groups.get(name, []):
            if member in groups:
                visit(member)
        active.pop()
        visited.add(name)

    for group in groups:
        visit(group)


def _toml_key(value: str) -> str:
    return value if NAME_RE.fullmatch(value) else _toml_string(value)


def _toml_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'
