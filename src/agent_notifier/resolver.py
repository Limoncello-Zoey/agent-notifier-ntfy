"""Resolve configured topic and group names to concrete ntfy topics."""

from __future__ import annotations

from .config import Config
from .errors import ResolutionError


def resolve_target(config: Config, target: str | None = None) -> tuple[str, list[str]]:
    """Return the effective target name and stable, de-duplicated ntfy topics."""
    effective = target if target is not None else config.defaults.target
    if effective is None:
        raise ResolutionError("未提供目标，且配置中没有 defaults.target")
    if effective not in config.topics and effective not in config.groups:
        raise ResolutionError(f"目标不存在: {effective}")

    resolved: list[str] = []
    seen_topics: set[str] = set()
    active: list[str] = []

    def visit(name: str) -> None:
        if name in config.topics:
            topic = config.topics[name]
            if topic not in seen_topics:
                seen_topics.add(topic)
                resolved.append(topic)
            return
        if name in active:
            start = active.index(name)
            cycle = active[start:] + [name]
            raise ResolutionError(f"话题组存在循环引用: {' -> '.join(cycle)}")
        members = config.groups.get(name)
        if members is None:
            raise ResolutionError(f"目标不存在: {name}")
        active.append(name)
        for member in members:
            visit(member)
        active.pop()

    visit(effective)
    return effective, resolved
