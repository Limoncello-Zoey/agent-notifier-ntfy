import pytest

from agent_notifier.config import Config
from agent_notifier.errors import NotificationError
from agent_notifier.notification import notification_from_mapping, normalize_notification


def test_normalizes_whitespace_defaults_and_tags() -> None:
    item = normalize_notification(
        Config(), emoji=" ✅ ", title=" Done ", message=" Body ", tags=["one", "one", "two"]
    )
    assert item.rendered_title == "✅ Done"
    assert item.message == "Body"
    assert item.priority == 4
    assert item.tags == ("one", "two")


@pytest.mark.parametrize(
    "kwargs, match",
    [
        ({"emoji": " ", "title": "t", "message": "m"}, "emoji"),
        ({"emoji": "x", "title": "t", "message": "m", "priority": True}, "priority"),
        ({"emoji": "x", "title": "t" * 256, "message": "m"}, "256"),
        ({"emoji": "x", "title": "t", "message": "m" * 3501}, "3500"),
        ({"emoji": "x", "title": "t", "message": "m", "tags": ["a,b"]}, "逗号"),
        ({"emoji": "✅", "title": "✅ Done", "message": "m"}, "重复"),
        ({"emoji": "x", "title": "t", "message": "m", "tags": {"a"}}, "数组"),
    ],
)
def test_rejects_invalid_notification(kwargs: dict, match: str) -> None:
    with pytest.raises(NotificationError, match=match):
        normalize_notification(Config(), **kwargs)


def test_mapping_requires_public_fields_and_rejects_unknown_fields() -> None:
    with pytest.raises(NotificationError, match="缺少"):
        notification_from_mapping(Config(), {"message": "m"})
    with pytest.raises(NotificationError, match="未知字段"):
        notification_from_mapping(
            Config(),
            {"emoji": "x", "title": "t", "message": "m", "priority": 4, "secret": 1},
        )
