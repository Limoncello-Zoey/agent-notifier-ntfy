"""Public ``agent-notifier`` command-line interface."""

from __future__ import annotations

import argparse
from dataclasses import replace
import json
import logging
import sys
from typing import Sequence

from . import __version__
from .client import notify_daemon
from .config import (
    Config,
    DefaultsConfig,
    config_path,
    init_config,
    load_config,
    render_config,
    save_config,
    validate_config,
)
from .daemon import run_daemon
from .diagnostics import doctor_checks
from .errors import AgentNotifierError
from .mcp_server import run_mcp
from .notification import normalize_notification
from .resolver import resolve_target


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-notifier")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    config = commands.add_parser("config", help="管理配置文件")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    for name in ("init", "path", "show", "validate"):
        config_sub.add_parser(name)

    topic = commands.add_parser("topic", help="管理话题")
    topic_sub = topic.add_subparsers(dest="topic_command", required=True)
    topic_sub.add_parser("list")
    topic_set = topic_sub.add_parser("set")
    topic_set.add_argument("name")
    topic_set.add_argument("ntfy_topic")
    topic_remove = topic_sub.add_parser("remove")
    topic_remove.add_argument("name")

    group = commands.add_parser("group", help="管理话题组")
    group_sub = group.add_subparsers(dest="group_command", required=True)
    group_sub.add_parser("list")
    for name in ("show", "remove", "resolve"):
        item = group_sub.add_parser(name)
        item.add_argument("name")
    group_set = group_sub.add_parser("set")
    group_set.add_argument("name")
    group_set.add_argument("members", nargs="*")

    default = commands.add_parser("default", help="管理默认目标")
    default_sub = default.add_subparsers(dest="default_command", required=True)
    default_sub.add_parser("show")
    default_set = default_sub.add_parser("set")
    default_set.add_argument("target")
    default_sub.add_parser("clear")

    send = commands.add_parser("send", help="通过本地守护进程发送通知")
    send.add_argument("target", nargs="?")
    send.add_argument("--emoji", default="ℹ️")
    send.add_argument("--message", required=True)
    send.add_argument("--title", default="Agent Notifier")
    send.add_argument("--priority", type=int, choices=(4, 5))
    send.add_argument("--tag", action="append", default=[])
    send.add_argument("--json", action="store_true")

    commands.add_parser("daemon", help="前台运行通知守护进程")
    commands.add_parser("mcp", help="运行 STDIO MCP Server")
    doctor = commands.add_parser("doctor", help="检查本地安装与运行状态")
    doctor.add_argument("--json", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "config":
            return _config_command(args)
        if args.command == "topic":
            return _topic_command(args)
        if args.command == "group":
            return _group_command(args)
        if args.command == "default":
            return _default_command(args)
        if args.command == "send":
            return _send_command(args)
        if args.command == "daemon":
            logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(message)s")
            run_daemon(load_config())
            return 0
        if args.command == "mcp":
            run_mcp(load_config())
            return 0
        if args.command == "doctor":
            return _doctor_command(args)
    except AgentNotifierError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("已停止", file=sys.stderr)
        return 130
    return 2


def _config_command(args: argparse.Namespace) -> int:
    if args.config_command == "path":
        print(config_path())
    elif args.config_command == "init":
        print(f"已创建配置: {init_config()}")
    elif args.config_command == "show":
        print(render_config(load_config()), end="")
    else:
        load_config()
        print(f"配置有效: {config_path()}")
    return 0


def _topic_command(args: argparse.Namespace) -> int:
    config = load_config()
    if args.topic_command == "list":
        for name, topic in config.topics.items():
            print(f"{name}\t{topic}")
        return 0
    if args.topic_command == "set":
        if args.name in config.groups:
            raise AgentNotifierError(f"同名话题组已存在: {args.name}")
        topics = dict(config.topics)
        topics[args.name] = args.ntfy_topic
        _save_validated(replace(config, topics=topics))
        print(f"已设置话题 {args.name}")
        return 0
    if args.name not in config.topics:
        raise AgentNotifierError(f"话题不存在: {args.name}")
    topics = dict(config.topics)
    del topics[args.name]
    _save_validated(replace(config, topics=topics))
    print(f"已删除话题 {args.name}")
    return 0


def _group_command(args: argparse.Namespace) -> int:
    config = load_config()
    command = args.group_command
    if command == "list":
        for name, members in config.groups.items():
            print(f"{name}\t{', '.join(members)}")
        return 0
    if command == "show":
        members = _require_group(config, args.name)
        print("\n".join(members))
        return 0
    if command == "resolve":
        _require_group(config, args.name)
        _, topics = resolve_target(config, args.name)
        print("\n".join(topics))
        return 0
    if command == "set":
        if args.name in config.topics:
            raise AgentNotifierError(f"同名话题已存在: {args.name}")
        groups = {name: list(members) for name, members in config.groups.items()}
        groups[args.name] = list(args.members)
        _save_validated(replace(config, groups=groups))
        print(f"已设置话题组 {args.name}")
        return 0
    _require_group(config, args.name)
    groups = {name: list(members) for name, members in config.groups.items()}
    del groups[args.name]
    _save_validated(replace(config, groups=groups))
    print(f"已删除话题组 {args.name}")
    return 0


def _default_command(args: argparse.Namespace) -> int:
    config = load_config()
    if args.default_command == "show":
        print(config.defaults.target or "(未设置)")
        return 0
    target = args.target if args.default_command == "set" else None
    updated = replace(config, defaults=DefaultsConfig(target, config.defaults.priority))
    _save_validated(updated)
    print(f"默认目标已设置为 {target}" if target else "已清除默认目标")
    return 0


def _send_command(args: argparse.Namespace) -> int:
    config = load_config()
    notification = normalize_notification(
        config,
        target=args.target,
        emoji=args.emoji,
        title=args.title,
        message=args.message,
        priority=args.priority,
        tags=args.tag,
    )
    result = notify_daemon(config, notification)
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(
            f"状态: {result.status}；目标: {result.target}；"
            f"成功: {result.sent}；失败: {result.failed}"
        )
        for item in result.results:
            suffix = item.message_id if item.ok else item.error
            print(f"- {item.topic}: {'成功' if item.ok else '失败'}{f' ({suffix})' if suffix else ''}")
    return 0 if result.status in {"success", "skipped"} else 1


def _doctor_command(args: argparse.Namespace) -> int:
    checks = doctor_checks()
    result = {"ok": all(item["ok"] for item in checks), "checks": checks}
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for item in checks:
            print(f"[{'通过' if item['ok'] else '失败'}] {item['name']}: {item['detail']}")
    return 0 if result["ok"] else 1


def _require_group(config: Config, name: str) -> list[str]:
    try:
        return config.groups[name]
    except KeyError as exc:
        raise AgentNotifierError(f"话题组不存在: {name}") from exc


def _save_validated(config: Config) -> None:
    save_config(validate_config(config))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
