from dataclasses import replace

import pytest

from agent_notifier.config import Config, DefaultsConfig
from agent_notifier.errors import ResolutionError
from agent_notifier.resolver import resolve_target


def sample_config() -> Config:
    return replace(
        Config(),
        defaults=DefaultsConfig("second", 4),
        topics={"a": "topic-a", "alias-a": "topic-a", "b": "topic-b"},
        groups={
            "first": ["a", "b"],
            "second": ["first", "alias-a"],
            "silent": [],
        },
    )


def test_topic_resolves_directly() -> None:
    assert resolve_target(sample_config(), "b") == ("b", ["topic-b"])


def test_nested_groups_preserve_first_seen_order_and_deduplicate_real_topics() -> None:
    assert resolve_target(sample_config()) == ("second", ["topic-a", "topic-b"])


def test_empty_group_is_valid() -> None:
    assert resolve_target(sample_config(), "silent") == ("silent", [])


def test_nested_empty_groups_resolve_to_no_topics() -> None:
    config = replace(Config(), groups={"empty": [], "nested": ["empty"]})
    assert resolve_target(config, "nested") == ("nested", [])


def test_unknown_target_never_falls_back_to_public_topic_name() -> None:
    with pytest.raises(ResolutionError, match="目标不存在"):
        resolve_target(sample_config(), "typo")


def test_missing_default_is_rejected() -> None:
    with pytest.raises(ResolutionError, match="没有 defaults.target"):
        resolve_target(Config())


def test_runtime_cycle_guard_reports_path() -> None:
    config = replace(Config(), groups={"a": ["b"], "b": ["a"]})
    with pytest.raises(ResolutionError, match=r"a -> b -> a"):
        resolve_target(config, "a")
