from dataclasses import replace
import json
import subprocess

import pytest

from agent_notifier.config import Config, ServerConfig
from agent_notifier.errors import NotificationError
from agent_notifier.notification import normalize_notification
from agent_notifier.sender import NtfySender


def make_config() -> Config:
    return replace(
        Config(),
        server=ServerConfig("https://example.test"),
        topics={"a": "topic/a", "b": "topic-b"},
        groups={"all": ["a", "b"], "silent": []},
    )


def note(target: str = "all"):
    return normalize_notification(
        make_config(), target=target, emoji="✅", title="Done", message="Body", tags=["codex"]
    )


def completed(returncode: int, payload: dict, *, stderr: str = ""):
    return subprocess.CompletedProcess([], returncode, json.dumps(payload), stderr)


def test_sends_each_topic_with_argument_array_and_parses_ids() -> None:
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return completed(0, {"event": "message", "id": f"id-{len(calls)}"})

    result = NtfySender(make_config(), executable="/usr/bin/ntfy", runner=runner).send(note())
    assert result.status == "success"
    assert result.sent == 2
    assert [item.message_id for item in result.results] == ["id-1", "id-2"]
    assert calls[0][0] == [
        "/usr/bin/ntfy", "publish", "--title", "✅ Done", "--priority", "4",
        "--tags", "codex", "https://example.test/topic%2Fa", "Body",
    ]
    assert calls[0][1]["check"] is False


def test_empty_group_skips_without_looking_for_executable() -> None:
    result = NtfySender(make_config()).send(note("silent"))
    assert result.status == "skipped"
    assert result.results == ()


def test_server_error_is_failure_even_with_zero_exit_code_and_other_topics_continue() -> None:
    responses = iter([
        completed(0, {"code": 50001, "http": 400, "error": "bad request"}),
        completed(0, {"event": "message", "id": "ok"}),
    ])
    result = NtfySender(make_config(), executable="ntfy", runner=lambda *a, **k: next(responses)).send(note())
    assert result.status == "partial_failure"
    assert result.sent == 1
    assert result.failed == 1


def test_retries_429_and_5xx_with_bounded_backoff() -> None:
    sleeps = []
    responses = iter([
        completed(1, {"http": 429, "error": "limited"}),
        completed(1, {"http": 503, "error": "unavailable"}),
        completed(0, {"event": "message", "id": "ok"}),
    ])
    sender = NtfySender(
        replace(make_config(), groups={}),
        executable="ntfy",
        runner=lambda *a, **k: next(responses),
        sleeper=sleeps.append,
    )
    result = sender.send(note("a"))
    assert result.status == "success"
    assert sleeps == [1.0, 2.0]


def test_invalid_success_json_is_not_accepted() -> None:
    sender = NtfySender(
        make_config(), executable="ntfy", runner=lambda *a, **k: completed(0, {"ok": True})
    )
    result = sender.send(note("a"))
    assert result.status == "failed"
    assert "event=message" in result.results[0].error


def test_all_topics_can_fail_without_fail_fast() -> None:
    calls = []

    def runner(*args, **kwargs):
        calls.append(args[0])
        return completed(2, {"http": 400, "error": "rejected"})

    result = NtfySender(make_config(), executable="ntfy", runner=runner).send(note())
    assert result.status == "failed"
    assert result.sent == 0 and result.failed == 2
    assert len(calls) == 2


def test_timeout_is_bounded_and_remaining_topics_continue() -> None:
    calls = 0

    def runner(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])
        return completed(0, {"event": "message", "id": "second"})

    result = NtfySender(make_config(), executable="ntfy", runner=runner, timeout=1).send(note())
    assert result.status == "partial_failure"
    assert "超过 1 秒" in result.results[0].error
    assert result.results[1].message_id == "second"


def test_special_characters_remain_single_arguments() -> None:
    observed = []
    item = normalize_notification(
        make_config(),
        target="a",
        emoji="🧪",
        title="$(touch nope)",
        message="hello; echo unsafe\n中文",
        tags=["one", "two"],
    )
    sender = NtfySender(
        make_config(),
        executable="ntfy",
        runner=lambda command, **kwargs: observed.append(command)
        or completed(0, {"event": "message", "id": "ok"}),
    )
    sender.send(item)
    assert observed[0][-1] == "hello; echo unsafe\n中文"
    assert observed[0][3] == "🧪 $(touch nope)"
    assert observed[0][7] == "one,two"


def test_missing_executable_is_actionable(monkeypatch) -> None:
    monkeypatch.setattr("agent_notifier.sender.shutil.which", lambda value: None)
    with pytest.raises(NotificationError, match="PATH"):
        NtfySender(make_config()).send(note("a"))
