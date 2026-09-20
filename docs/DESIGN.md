# Agent Notifier 设计方案

## 1. 目标

构建一个面向 Agent 的 ntfy 通知系统：Agent 通过紧凑、结构化的 MCP Tool 发送有语义的任务通知；独立后台进程通过 Codex OpenTelemetry 事件监测模型访问与响应流故障。两条路径共用配置、目标解析和发送核心。

核心目标：

- Agent 只需要调用一个 `ntfy_send` Tool，不需要了解 Shell 转义、配置文件格式或话题组展开逻辑。
- 用户可以通过 CLI 管理配置，也可以直接编辑配置文件。
- 单话题与话题组在发送接口中具有完全相同的使用方式。
- 话题组支持递归嵌套、循环检测、最终话题去重和空组静默成功。
- Agent 环内通知使用本地 STDIO MCP Server。
- Agent 环外仅监测模型 API、SSE 和 WebSocket 链路故障，不监测 Codex 进程退出、崩溃、PID 或心跳。
- 使用本地 OTLP/HTTP JSON 接收器接收 Codex 官方结构化遥测，不解析不稳定的 transcript/JSONL 文件。
- 复用已安装的 `ntfy` CLI，第一版不重复实现 ntfy HTTP 客户端。

## 2. 已锁定的架构

```text
Codex Agent ──MCP ntfy_send────────────────────┐
                                               │
Codex OTel ──OTLP/HTTP JSON──> agent-notifierd ├──> 共享业务核心
                                               │    配置/解析/发送
用户 CLI ──────────────────────────────────────┘          │
                                                         ▼
                                                /usr/bin/ntfy publish
                                                         │
                                                         ▼
                                                  https://ntfy.sh
```

实现语言采用 Python 3，优先只使用标准库。MCP Server 使用依赖无关的 STDIO JSON-RPC 实现，与本机现有 MCP Server 风格保持一致。

项目建议结构：

```text
agent_notifier/
├── DESIGN.md
├── README.md
├── pyproject.toml
├── src/agent_notifier/
│   ├── __init__.py
│   ├── cli.py
│   ├── config.py
│   ├── resolver.py
│   ├── sender.py
│   ├── mcp_server.py
│   ├── otlp_receiver.py
│   ├── failure_monitor.py
│   └── daemon.py
└── tests/
    ├── test_config.py
    ├── test_resolver.py
    ├── test_sender.py
    ├── test_cli.py
    ├── test_mcp_server.py
    ├── test_otlp_receiver.py
    └── test_failure_monitor.py
```

## 3. 运行生命周期

系统包含两个生命周期彼此独立的进程。

### 3.1 Agent 环内：STDIO MCP

1. Codex 启动或建立 MCP 连接时，按配置启动 `mcp_server.py` 本地进程。
2. MCP Server 在 Codex 主机存活期间等待 Tool Call，空闲时不执行发送逻辑。
3. Agent 调用 `ntfy_send` 后，MCP Server 解析目标并为每个最终话题执行一次 `ntfy publish`。
4. 每个 `ntfy publish` 都是短生命周期子进程，发送完成即退出。
5. Codex 主机退出或断开连接后，STDIO MCP Server 随之退出。

### 3.2 Agent 环外：模型链路故障监测

1. `agent-notifierd` 由 `systemd --user` 启动并常驻，绑定 `127.0.0.1`，不暴露到局域网。
2. Codex 在用户级 `~/.codex/config.toml` 中启用 OTLP/HTTP JSON 日志导出，并关闭用户提示词正文导出。
3. 守护进程接收 `codex.api_request`、`codex.sse_event`、`codex.websocket_request` 和 `codex.websocket_event` 等结构化事件。
4. 失败事件先进入短暂宽限窗口；同一会话随后出现成功事件时取消告警，避免把自动重试误报为最终故障。
5. 宽限期后仍未恢复时，守护进程通过共享发送核心发出优先级 `5` 的模型链路异常通知。
6. 相同故障在去重窗口内只通知一次；恢复后再次失败可重新通知。

明确不实现：Codex PID 监测、父进程死亡检测、心跳、崩溃推断、终端关闭检测、WSL 关闭检测和 `agent-notifier run -- codex` 包装入口。这些能力在当前实际使用场景中没有收益，按 YAGNI 删除。

推荐的 Codex 用户级配置：

```toml
[otel]
environment = "local"
log_user_prompt = false
exporter = { otlp-http = {
  endpoint = "http://127.0.0.1:4318/v1/logs",
  protocol = "json"
} }
```

本机的 `ntfy.service` 不参与该流程，无需启用。它只用于自托管 ntfy 服务端；本项目使用远程 `ntfy.sh`。

## 4. 配置设计

### 4.1 配置位置

默认配置文件：

```text
~/.config/agent-notifier/config.toml
```

允许通过环境变量覆盖：

```text
AGENT_NOTIFIER_CONFIG=/absolute/path/config.toml
```

CLI 必须提供配置文件定位入口：

```bash
agent-notifier config path
```

配置修改采用原子写入，避免中断导致文件损坏。CLI 不隐藏配置格式，用户可以直接编辑 TOML，并通过 `config validate` 校验。

### 4.2 配置格式

```toml
version = 1

[server]
base_url = "https://ntfy.sh"

[defaults]
target = "zoey"
priority = 4

[monitor]
listen_host = "127.0.0.1"
listen_port = 4318
failure_grace_seconds = 30
dedupe_window_seconds = 300

[topics]
zoey = "nankai_limoncello_zoey_watch"
builds = "limoncello_builds"
alerts = "limoncello_alerts"

[groups]
daily = ["zoey", "builds"]
all = ["daily", "alerts"]
silent = []
```

### 4.3 配置不变量

- `topics` 与 `groups` 共享同一名称空间。
- 任何话题和话题组都不允许重名。
- 名称必须非空，并限制为便于 CLI 使用的标识符：字母、数字、点、下划线和短横线。
- `defaults.target` 必须引用存在的话题或话题组；允许不配置默认目标。
- `defaults.priority` 必须为 `1..5`，默认值为 `4`。
- `monitor.listen_host` 第一版只允许回环地址，默认 `127.0.0.1`。
- `monitor.listen_port` 必须为有效 TCP 端口，默认 `4318`。
- 故障宽限期与去重窗口必须为正整数；默认分别为 `30` 秒和 `300` 秒。
- 每个组成员必须引用已存在的话题或话题组。
- 配置加载时必须对全部组执行循环检测，存在循环时整个配置无效。
- 未知字段按错误处理，避免拼写错误被静默忽略。

## 5. 目标与话题组解析

发送接口统一使用 `target`。调用方不需要区分目标是话题还是话题组。

解析规则：

1. 如果目标是话题，直接得到该话题。
2. 如果目标是话题组，按成员声明顺序执行深度优先递归解析。
3. 递归过程中维护当前访问栈；再次进入栈内名称即判定为循环，并报告完整循环路径。
4. 最终话题按首次出现顺序去重，避免嵌套组导致重复发送。
5. 空组或最终解析为零话题的组返回静默成功，不执行网络请求。
6. 目标不存在时返回明确错误，不回退到原始 ntfy 话题名，避免拼写错误发送到意外的公开话题。

示例：

```toml
[topics]
a = "topic-a"
b = "topic-b"

[groups]
first = ["a", "b"]
second = ["first", "a"]
```

`second` 最终解析结果为：

```text
["topic-a", "topic-b"]
```

## 6. CLI 公共接口

可执行文件名称：

```text
agent-notifier
```

### 6.1 配置管理

```bash
agent-notifier config init
agent-notifier config path
agent-notifier config show
agent-notifier config validate
```

- `config init`：配置不存在时创建带注释的最小模板；已存在时拒绝覆盖。
- `config path`：输出当前生效配置的绝对路径。
- `config show`：输出规范化配置，不输出未来可能加入的凭据值。
- `config validate`：校验 Schema、名称唯一性、引用完整性和循环。

### 6.2 话题管理

```bash
agent-notifier topic list
agent-notifier topic set NAME NTFY_TOPIC
agent-notifier topic remove NAME
```

`set` 为幂等更新；如果同名组已存在则拒绝操作。

### 6.3 话题组管理

```bash
agent-notifier group list
agent-notifier group show NAME
agent-notifier group set NAME [MEMBER ...]
agent-notifier group remove NAME
agent-notifier group resolve NAME
```

- `group set NAME` 不带成员时创建空组。
- `group resolve` 输出最终去重后的真实 ntfy 话题列表，便于调试嵌套关系。
- 修改后必须校验全部引用和循环；校验失败时不写入配置。

### 6.4 默认目标管理

```bash
agent-notifier default show
agent-notifier default set TARGET
agent-notifier default clear
```

### 6.5 手动发送

```bash
agent-notifier send [TARGET] \
  --message "任务已经完成" \
  --title "Codex 通知" \
  --priority 4 \
  --tag white_check_mark
```

- `TARGET` 省略时使用 `defaults.target`。
- `--message` 必填。
- `--title` 可选。
- `--priority` 可选，未提供时使用配置默认值。
- `--tag` 可重复；内部转换为 ntfy 的逗号分隔标签。
- CLI 输出与 MCP 使用相同的结构化结果；终端默认展示人类可读摘要，并提供 `--json` 输出完整 JSON。

### 6.6 守护进程入口

```bash
agent-notifier daemon
```

该命令以前台方式运行 OTLP/HTTP JSON 接收器，日志写入 stderr，便于 `systemd --user` 管理。它不负责 fork、写 PID 文件或自行后台化。

建议用户服务：

```ini
[Unit]
Description=Agent Notifier model transport monitor

[Service]
ExecStart=%h/.local/bin/agent-notifier daemon
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
```

## 7. MCP Tool 接口

MCP Server 名称：

```text
agent-notifier
```

只暴露一个 Tool，控制上下文占用：

```text
ntfy_send
```

输入 Schema：

```json
{
  "type": "object",
  "properties": {
    "target": {
      "type": "string",
      "description": "已配置的话题或话题组名称；省略时使用默认目标。"
    },
    "message": {
      "type": "string",
      "minLength": 1,
      "description": "通知正文。"
    },
    "title": {
      "type": "string",
      "description": "可选通知标题。"
    },
    "priority": {
      "type": "integer",
      "minimum": 1,
      "maximum": 5,
      "description": "可选优先级；省略时使用配置默认值。"
    },
    "tags": {
      "type": "array",
      "items": {"type": "string"},
      "description": "可选 ntfy 标签或 emoji 名称。"
    }
  },
  "required": ["message"],
  "additionalProperties": false
}
```

Tool annotations：

- `readOnlyHint = false`
- `destructiveHint = false`
- `idempotentHint = false`
- `openWorldHint = true`

Codex 侧应将该 Tool 配置为自动批准，以支持任务停止前的无人值守通知；只启用 `ntfy_send`，避免无关工具进入上下文。

## 8. 发送行为与返回结果

发送实现使用无 Shell 的参数数组调用，禁止拼接 Shell 命令：

```text
/usr/bin/ntfy publish
  --title TITLE
  --priority PRIORITY
  --tags TAGS
  https://ntfy.sh/TOPIC
  MESSAGE
```

批量发送规则：

- 按最终话题顺序逐个发送。
- 单个话题失败后继续发送其余话题，不采用 fail-fast。
- 收集每个话题的返回码、服务端消息 ID和错误摘要。
- 不把完整环境变量、认证信息或无界 stdout/stderr 返回给模型。

统一结果示例：

```json
{
  "status": "success",
  "target": "all",
  "resolved_topics": ["topic-a", "topic-b"],
  "sent": 2,
  "failed": 0,
  "results": [
    {"topic": "topic-a", "ok": true, "message_id": "abc"},
    {"topic": "topic-b", "ok": true, "message_id": "def"}
  ]
}
```

状态定义：

- `success`：所有最终话题发送成功。
- `partial_failure`：至少一个成功且至少一个失败。
- `failed`：存在最终话题，但全部发送失败。
- `skipped`：最终解析结果为空，没有执行网络请求。

`skipped` 是正常成功结果：

```json
{
  "status": "skipped",
  "target": "silent",
  "resolved_topics": [],
  "sent": 0,
  "failed": 0,
  "results": []
}
```

## 9. Agent 自动通知策略

MCP Tool 只提供“发送通知”的能力，不负责判断 Agent 何时停止。停止前通知属于 Agent 策略，应由全局 `AGENTS.md`、Skill 或未来的 Hook 负责触发。

推荐的全局规则语义：

```text
除用户硬性打断外，在任何正常交还控制权、任务完成、任务阻塞、
计划决策或命令完成前，调用 ntfy_send。根据状态在优先级 4 和 5
之间选择；通知失败不得掩盖原任务结果，但必须在最终答复中说明。
```

建议状态映射：

- 正常完成、等待输入、普通暂停：优先级 `4`。
- 严重失败、需要立即人工干预、不可恢复阻塞：优先级 `5`。

## 10. 模型链路故障策略

模型链路监测使用 Codex 官方 OpenTelemetry 日志，而不是 Hooks、`notify`、App Server 或 transcript 文件：

- Hooks 用于生命周期动作，但不提供稳定、完整的模型传输错误分类。
- `notify` 当前只支持 `agent-turn-complete`，适合完成通知，不足以覆盖模型无法访问。
- App Server 能提供精确的 `error` 和 `turn/completed(status=failed)`，但只有当调用方本身通过该 App Server 驱动会话时才能观察事件，不能作为任意既有 Codex 客户端的旁路监听器。
- transcript 路径和内容格式不是稳定接口，不作为监控协议。
- OTel 原生提供 API 请求、SSE 和 WebSocket 的成功状态、HTTP 状态及错误信息，最符合当前范围。

第一版只处理以下事件族：

- `codex.api_request`：请求失败、HTTP 4xx/5xx、连接失败。
- `codex.sse_event`：响应流处理失败或断开。
- `codex.websocket_request`：WebSocket 请求失败。
- `codex.websocket_event`：WebSocket 消息处理失败。

处理规则：

1. 按会话 ID 和传输类型维护待确认故障。
2. 失败事件刷新待确认故障及最后错误摘要。
3. 同一会话和传输随后出现成功事件时清除待确认故障。
4. 待确认故障超过 `failure_grace_seconds` 后发送一次优先级 `5` 通知。
5. 消息只包含模型、传输类型、HTTP 状态、尝试次数和经过截断/清洗的错误摘要，不包含用户提示词、模型输出或认证信息。
6. 相同故障指纹在 `dedupe_window_seconds` 内抑制重复通知。

这是一种可靠的“最终未恢复”近似判断，而不是对 Codex 内部重试状态机的复制。若后续证据表明 OTel 事件不足，再考虑将 App Server 适配器作为可选增强；第一版不实现。

## 11. 错误处理与安全边界

- 配置不存在：返回可操作错误，并提示运行 `agent-notifier config init`。
- 默认目标缺失且调用未提供目标：拒绝发送。
- 目标不存在：拒绝发送，不将未知名称直接当作公开 ntfy 话题。
- 循环引用：配置整体无效，报告循环路径。
- `ntfy` 不存在：报告期望的可执行文件和 PATH，不自动安装软件。
- 子进程超时：终止该次发送并继续处理剩余话题。
- 消息、标题和标签设置合理长度上限，避免意外发送超大内容。
- 不使用 `shell=True`，避免命令注入。
- OTLP 接收器只监听回环地址，并限制请求体大小、HTTP 方法和内容类型。
- OTel 配置保持 `log_user_prompt = false`；守护进程不落盘保存原始事件正文。
- 遥测解析失败只记录有界错误，不影响 Codex 本身，也不触发通知风暴。
- 第一版不实现用户名、密码、Token、附件、邮件转发和延迟发送，遵循 YAGNI。

## 12. 测试方案

### 12.1 单元测试

- 话题与组名称跨类型重复时拒绝配置。
- 一层组、多层组、重复引用按首次出现顺序去重。
- 直接循环、自循环和多节点循环均能报告完整路径。
- 空组和嵌套后为空的组解析为零话题。
- 缺失成员、缺失默认目标、非法优先级和未知字段均报错。
- CLI 修改配置失败时原文件保持不变。

### 12.2 发送测试

- 使用假的 `ntfy` 可执行文件验证参数数组，不访问真实网络。
- 单话题成功、全部成功、部分失败、全部失败和超时结果正确。
- 标题缺失、多个标签、中文消息和特殊字符不会产生 Shell 注入或错误拆词。
- 空组不启动 `ntfy` 子进程。

### 12.3 MCP 协议测试

- `initialize` 返回正确服务信息和 Tool capability。
- `tools/list` 只暴露 `ntfy_send`。
- `tools/call` 参数验证与 CLI 业务规则一致。
- stdout 只输出 MCP JSON-RPC，诊断日志只写 stderr。
- Tool 返回内容有界，不泄露环境变量和配置敏感值。

### 12.4 验收测试

1. 配置真实话题 `zoey = "nankai_limoncello_zoey_watch"`。
2. 创建包含 `zoey` 的嵌套话题组。
3. 通过 CLI 向单话题和话题组发送测试消息。
4. 在 Codex 中调用 `ntfy_send`，确认工具列表、参数和返回结果正确。
5. 使用空组调用，确认静默返回 `skipped` 且没有消息产生。
6. 构造循环组，确认配置校验失败且不执行发送。

### 12.5 模型故障监测测试

- OTLP/HTTP JSON 请求解析成功，并对非法方法、过大请求体和非 JSON 内容返回明确错误。
- 单次失败进入宽限期，不立即通知。
- 宽限期内出现成功事件会取消待发送通知。
- 连续失败且未恢复时只发送一次优先级 `5` 通知。
- 去重窗口内的相同错误被抑制，不同会话或不同错误可以独立通知。
- 原始事件中的提示词、输出、Token 和无界错误正文不会进入 ntfy 消息。

## 13. 完成标准

- 用户可以通过 CLI 或直接编辑 TOML 管理话题和递归话题组。
- 配置能够确定性检测重名、缺失引用和循环。
- CLI 与 MCP 共用同一业务实现，不复制解析或发送逻辑。
- Agent 只需了解单个 `ntfy_send` Tool。
- Agent 能继续提供带任务语义的环内通知；后台守护进程仅兜底模型 API 与响应流故障。
- 单话题、话题组和空话题组在同一接口下行为明确。
- MCP Server 随 Codex 生命周期运行；`agent-notifierd` 作为独立 `systemd --user` 服务运行。
- 不依赖 Codex PID、包装命令、心跳、App Server 代理或 transcript 解析。
- 本机 `ntfy.service` 保持禁用也能正常发送到 `ntfy.sh`。
- 自动化测试不依赖公网；真实 ntfy.sh 仅用于最终人工验收。
