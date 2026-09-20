"""Canonical notification model shared by every ingress path."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .config import Config
from .errors import NotificationError


@dataclass(frozen=True, slots=True)
class Notification:
    target: str | None
    emoji: str
    title: str
    message: str
    priority: int
    tags: tuple[str, ...] = ()

    @property
    def rendered_title(self) -> str:
        return f"{self.emoji} {self.title}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "emoji": self.emoji,
            "title": self.title,
            "message": self.message,
            "priority": self.priority,
            "tags": list(self.tags),
        }


def normalize_notification(
    config: Config,
    *,
    target: object = None,
    emoji: object,
    title: object,
    message: object,
    priority: object = None,
    tags: object = None,
) -> Notification:
    normalized_target = _optional_text(target, "target")
    normalized_emoji = _required_text(emoji, "emoji")
    normalized_title = _required_text(title, "title")
    normalized_message = _required_text(message, "message")
    normalized_priority = config.defaults.priority if priority is None else priority
    if type(normalized_priority) is not int or normalized_priority not in (4, 5):
        raise NotificationError("priority 必须为 4 或 5")
    normalized_tags = _normalize_tags(tags)

    rendered = f"{normalized_emoji} {normalized_title}"
    if normalized_title == normalized_emoji or normalized_title.startswith(
        normalized_emoji + " "
    ):
        raise NotificationError("title 不得重复包含 emoji 前缀")
    if len(rendered.encode("utf-8")) > 256:
        raise NotificationError("添加 emoji 后的 title 不能超过 256 UTF-8 字节")
    if len(normalized_message.encode("utf-8")) > 3500:
        raise NotificationError("message 不能超过 3500 UTF-8 字节")
    if len(",".join(normalized_tags).encode("utf-8")) > 400:
        raise NotificationError("tags 合计不能超过 400 UTF-8 字节")

    return Notification(
        normalized_target,
        normalized_emoji,
        normalized_title,
        normalized_message,
        normalized_priority,
        normalized_tags,
    )


def notification_from_mapping(config: Config, raw: Mapping[str, Any]) -> Notification:
    allowed = {"target", "emoji", "title", "message", "priority", "tags"}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise NotificationError(f"通知包含未知字段: {', '.join(unknown)}")
    missing = [name for name in ("emoji", "title", "message", "priority") if name not in raw]
    if missing:
        raise NotificationError(f"通知缺少必填字段: {', '.join(missing)}")
    return normalize_notification(config, **raw)


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise NotificationError(f"{name} 必须是非空字符串")
    return value.strip()


def _optional_text(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, name)


def _normalize_tags(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise NotificationError("tags 必须是字符串数组")
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        tag = _required_text(item, "tag")
        if "," in tag:
            raise NotificationError("单个 tag 不能包含逗号")
        if tag not in seen:
            seen.add(tag)
            result.append(tag)
    return tuple(result)
