from dataclasses import replace
from pathlib import Path

import pytest

from agent_notifier.config import (
    Config,
    ConfigError,
    DefaultsConfig,
    config_path,
    init_config,
    load_config,
    parse_config,
    save_config,
)


def test_defaults_and_environment_path(tmp_path: Path) -> None:
    path = tmp_path / "custom.toml"
    assert config_path({"AGENT_NOTIFIER_CONFIG": str(path)}) == path
    config = parse_config({})
    assert config.defaults.priority == 4
    assert config.monitor.listen_port == 4318


def test_relative_override_is_rejected() -> None:
    with pytest.raises(ConfigError, match="绝对路径"):
        config_path({"AGENT_NOTIFIER_CONFIG": "relative.toml"})


@pytest.mark.parametrize(
    "raw, message",
    [
        ({"typo": 1}, "未知字段"),
        ({"defaults": {"priority": 3}}, "4 或 5"),
        ({"monitor": {"listen_host": "0.0.0.0"}}, "回环"),
        ({"topics": {"same": "a"}, "groups": {"same": []}}, "重名"),
        ({"groups": {"a": ["missing"]}}, "不存在"),
        ({"topics": {"bad name": "a"}}, "必须由"),
        ({"server": {"base_url": "https://user:secret@example.test"}}, "用户名或密码"),
        ({"server": {"base_url": "https://example.test?q=secret"}}, "查询参数"),
    ],
)
def test_invalid_config_is_rejected(raw: dict, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        parse_config(raw)


def test_all_groups_are_checked_for_cycles() -> None:
    raw = {"groups": {"a": ["b"], "b": ["c"], "c": ["a"]}}
    with pytest.raises(ConfigError, match=r"a -> b -> c -> a"):
        parse_config(raw)


@pytest.mark.parametrize(
    "groups, path",
    [({"self": ["self"]}, "self -> self"), ({"a": ["b"], "b": ["a"]}, "a -> b -> a")],
)
def test_self_and_direct_cycles_report_complete_path(groups, path) -> None:
    with pytest.raises(ConfigError, match=path):
        parse_config({"groups": groups})


def test_save_and_load_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "config.toml"
    config = replace(
        Config(),
        defaults=DefaultsConfig("all", 5),
        topics={"one": "real-topic"},
        groups={"all": ["one"]},
    )
    save_config(config, path)
    assert load_config(path) == config


def test_init_never_overwrites(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    init_config(path)
    original = path.read_text()
    with pytest.raises(ConfigError, match="拒绝覆盖"):
        init_config(path)
    assert path.read_text() == original
