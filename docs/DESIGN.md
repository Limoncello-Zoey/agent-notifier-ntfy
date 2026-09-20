# Agent Notifier 设计方案

## 1. 目标

构建一个面向 Agent 的 ntfy 通知系统：Agent 通过紧凑、结构化的 MCP Tool 发送有语义的任务通知；独立后台进程通过 Codex OpenTelemetry 事件监测模型访问与响应流故障。两条路径共用配置、目标解析和发送核心。

核心目标：

- Agent 只需要调用一个 `ntfy_send` Tool，不需要了解 Shell 转义、配置文件格式或话题组展开逻辑。
- 用户可以通过 CLI 管理配置，也可以直接编辑配置文件。
- 单话题与话题组在发送接口中具有完全相同的使用方式。
- 话题组支持递归嵌套、循环检测、最终话题去重和空组静默成功。
- Agent 环内通知使用本地 STDIO MCP Server。
- Agent 在长程任务中每完成一个有意义、可验证的小点就发送进度通知；每次成功执行 `git commit` 后发送提交通知。
- Agent 环外仅监测模型 API、SSE 和 WebSocket 链路故障，不监测 Codex 进程退出、崩溃、PID 或心跳。
- 使用本地 OTLP/HTTP JSON 接收器接收 Codex 官方结构化遥测，不解析不稳定的 transcript/JSONL 文件。
- 复用已安装的 `ntfy` CLI，第一版不重复实现 ntfy HTTP 客户端。

## 2. 已锁定的架构

```text
Codex Agent ──MCP ntfy_send──────────────┐
                                         │
用户 CLI ────────────────────────────────┼──> 本地发送请求
                                         │
Codex OTel ──OTLP/HTTP JSON──────────────┤
                                         ▼
                                  agent-notifierd
                                  ├─ 故障状态监测
                                  └─ FIFO 待发送消息池
                                           │
                                           ▼
                                  共享配置/解析/发送核心
                                           │
                                           ▼
                                  /usr/bin/ntfy publish
                                           │
                                           ▼
                                    https://ntfy.sh
```

实现语言采用 Python 3，优先只使用标准库。MCP Server 使用依赖无关的 STDIO JSON-RPC 实现，与本机现有 MCP Server 风格保持一致。

项目建议结构：

```text
agent-notifier-ntfy/
├── README.md
├── docs/
│   └── DESIGN.md
├── pyproject.toml
├── src/agent_notifier/
│   ├── __init__.py
│   ├── cli.py
│   ├── config.py
│   ├── resolver.py
│   ├── sender.py
│   ├── send_queue.py
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

README 采用 Agent-first 布局：最前方依次提供用户可直接转发给 Agent 的一句话安装指令、CLI 配置入口和配置文件位置，随后提供供执行部署的 Agent 读取的自适应部署指南。项目运行代码未实现前必须明确标注设计阶段，不得展示无法执行的命令为已可用功能。

## 3. 运行生命周期

系统包含两个生命周期彼此独立的进程。

### 3.1 Agent 环内：STDIO MCP

1. Codex 启动或建立 MCP 连接时，按配置通过 `agent-notifier mcp` 启动本地 STDIO MCP 进程。
2. MCP Server 在 Codex 主机存活期间等待 Tool Call，空闲时不执行发送逻辑。
3. Agent 调用 `ntfy_send` 后，MCP Server 将发送请求提交到 `agent-notifierd` 的 FIFO 待发送消息池，并等待该请求的最终结果。
4. 守护进程中的单一发送 Worker 解析目标，并为每个最终话题执行一次 `ntfy publish`；每个 `ntfy publish` 都是短生命周期子进程，发送完成即退出。
5. Codex 主机退出或断开连接后，STDIO MCP Server 随之退出。

长程任务的阶段进度和 Git 提交通知都属于 Agent 策略层产生的环内通知：它们继续调用同一个 `ntfy_send` Tool，通过同一队列发送。守护进程不观察 Git 仓库，也不从 Shell 历史或文件变化猜测任务进度。

### 3.2 Agent 环外：模型链路故障监测

1. `agent-notifierd` 由 `systemd --user` 启动并常驻，绑定 `127.0.0.1`，不暴露到局域网。
2. Codex 在用户级 `~/.codex/config.toml` 中启用 OTLP/HTTP JSON 日志导出，并关闭用户提示词正文导出。
3. 守护进程接收 `codex.api_request`、`codex.sse_event`、`codex.websocket_request` 和 `codex.websocket_event` 等结构化事件。
4. 失败事件先进入短暂宽限窗口；同一会话随后出现成功事件时取消告警，避免把自动重试误报为最终故障。
5. 宽限期后仍未恢复时，守护进程通过共享发送核心发出优先级 `5` 的模型链路异常通知。
6. 相同故障在去重窗口内只通知一次；恢复后再次失败可重新通知。
7. 不同会话分别维护状态并分别发送通知；即使错误原因相同也不跨会话合并，因为模型服务不可用可能只影响部分会话，逐会话提示属于预期行为。

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

### 3.3 Agent 驱动的自适应部署

目标使用方式不是让用户复制一组 Shell 命令，也不是由项目提供一个假设所有机器环境相同的整体安装器。用户只需把 Git 地址和可选的 ntfy 话题交给本机 Agent；Agent 克隆仓库、阅读 README 中的部署提示，探测本机环境，并持续执行到部署和验收完成。

项目负责定义稳定的应用接口和目标状态：CLI 命令、配置格式、`agent-notifier mcp`、`agent-notifier daemon`、MCP Schema、OTel 端点及验收行为。部署 Agent 负责根据当前操作系统、架构、包管理器、Python 环境、Codex 配置和用户目录约定选择具体命令与安装路径。

部署 Agent 必须完成：

1. 只读探测 Linux/WSL2、架构、Python、Codex CLI、`systemd --user`、`ntfy` CLI、XDG 路径、`CODEX_HOME` 和现有配置；不支持的平台明确失败。
2. 选择适合本机的用户级 Python 安装方式，提供稳定 CLI 入口，且运行时不依赖克隆目录继续存在。
3. 初始化或无损合并应用配置。
4. 注册并启动单实例用户级 `agent-notifier.service`，禁止通过裸后台进程实现常驻。
5. 幂等注册并启用 `agent-notifier` STDIO MCP；只启用 `ntfy_send` 并将其 `approval_mode` 设为 `approve`。
6. 无损更新 Codex 用户级 `config.toml` 的 OTel 配置；修改前创建带时间戳的备份。
7. 在 Codex 实际读取的全局 `AGENTS.md` 或 `AGENTS.override.md` 中维护带起止标记的通知规则块，覆盖任务交还、长程小点进度和 Git 提交三类事件，并保留其他用户指令。
8. 执行 CLI、配置、systemd、MCP Schema、OTel、Agent 指令和真实 ntfy 消息的端到端验收。
9. 输出结构化部署摘要、实际路径、备份位置以及 Codex 重启提示。

部署过程必须幂等。系统包安装是唯一允许触发 `sudo` 的步骤；程序、配置、MCP 和守护进程均使用用户权限。现有 OTel 或全局 Agent 配置无法无损合并时不得静默覆盖，应返回明确冲突并由执行部署的 Agent 请求用户决策。

Codex 在启动时构建 MCP Tool 目录并读取全局 Agent 指令，因此当前执行部署的会话无法热加载新 Tool。守护进程在部署后立即可用；MCP 和通知触发规则从下一次 Codex 会话生效。CLI、IDE 扩展和 ChatGPT 桌面版分别需要新开会话、Restart extension 或 Restart MCP。

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
- `defaults.priority` 必须为 `4` 或 `5`，默认值为 `4`。
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
  --emoji "✅" \
  --message "任务已经完成" \
  --title "Codex 通知" \
  --priority 4 \
  --tag codex
```

- `TARGET` 省略时使用 `defaults.target`。
- `--emoji` 可选，直接传入一个 Unicode emoji；省略时使用 `ℹ️`。
- `--message` 必填。
- `--title` 可选，省略时规范化为 `Agent Notifier`。
- `--priority` 可选，未提供时使用配置默认值。
- `--tag` 可重复；内部转换为 ntfy 的逗号分隔标签。
- CLI 输出与 MCP 使用相同的结构化结果；终端默认展示人类可读摘要，并提供 `--json` 输出完整 JSON。

### 6.6 守护进程入口

```bash
agent-notifier daemon
```

该命令以前台方式运行 OTLP/HTTP JSON 接收器，日志写入 stderr，便于 `systemd --user` 管理。它不负责 fork、写 PID 文件或自行后台化。

守护进程在同一个回环 HTTP 服务上提供两个用途明确的端点：

```text
POST /v1/logs    Codex OTLP/HTTP JSON 日志入口
POST /v1/notify  MCP 与 CLI 的本地发送请求入口
```

`/v1/logs` 接收遥测后立即返回，故障监测产生的通知直接在进程内入队，不等待 ntfy 网络发送完成。`/v1/notify` 在请求入队后等待该任务完成，并返回统一结构化结果。两个端点都只绑定回环地址，并分别校验请求格式与请求体大小。

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

### 6.7 MCP Server 入口

```bash
agent-notifier mcp
```

该命令以前台 STDIO 模式运行 MCP Server，stdout 只用于 JSON-RPC，诊断信息只写入 stderr。Codex MCP 注册应指向这个稳定的已安装 CLI 入口，不得指向源码仓库中的 Python 文件，确保克隆目录被移动或删除后仍能启动。

## 7. 统一消息模型与 MCP Tool 接口

### 7.1 统一消息模型

环内、环外和 CLI 消息在进入待发送消息池前都必须规范化为同一个 `Notification` 对象：

```json
{
  "target": "zoey",
  "emoji": "✅",
  "title": "任务完成",
  "message": "设计文档已经更新，当前没有未解决的阻塞项。",
  "priority": 4,
  "tags": ["codex"]
}
```

字段定义：

- `target`：已配置的话题或话题组名称；省略时解析为默认目标。
- `emoji`：调用方根据通知内容自行选择的一个 Unicode emoji，发送层将其作为标题前缀。
- `title`：通知标题，概括“发生了什么”。
- `message`：通知正文，提供必要结果、状态或错误摘要；保持简报风格，不强制附加会话 ID、项目名等固定前缀。
- `priority`：通知优先级。本系统只使用 `4` 或 `5`。
- `tags`：可选的附加 ntfy 标签；按首次出现顺序去重，与主 emoji 相互独立。

统一校验规则：

- `emoji`、`title`、`message` 和 `priority` 在规范化后的对象中必须存在。
- `emoji` 去除首尾空白后不能为空；不限定候选集合或语义映射。
- `title` 去除首尾空白后不能为空；加上 emoji 前缀和一个空格后的最终标题，UTF-8 编码后不超过 `256` 字节。
- `message` 去除首尾空白后不能为空，UTF-8 编码后不超过 `3500` 字节，避免超过 ntfy 的 `4096` 字节消息上限后被自动当作附件。
- `priority` 必须为 `4` 或 `5`。
- 附加 tags 合计不超过 `400` UTF-8 字节，为 ntfy 的 `512` 字节 tags 上限预留余量。
- 消息正文使用纯文本；第一版不启用 Markdown、附件、点击动作、图标 URL 或延迟发送。

标题渲染规则固定为：

```text
{emoji} {normalized_title}
```

例如 `emoji = "✅"`、`title = "任务完成"` 最终发送为 `✅ 任务完成`。调用方不得在 `title` 中自行重复添加 emoji；发送层只负责添加一次前缀。

队列内部可以额外携带 `source` 和 `event` 元数据，例如 `agent/custom` 或 `monitor/rate_limited`，用于选择模板、诊断和测试。这些字段不是通知展示格式，也不暴露给 MCP Tool。

### 7.2 MCP Tool

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
    "emoji": {
      "type": "string",
      "minLength": 1,
      "description": "用于标题前缀的一个 emoji。"
    },
    "message": {
      "type": "string",
      "minLength": 1,
      "description": "通知正文。"
    },
    "title": {
      "type": "string",
      "minLength": 1,
      "description": "简短概括发生的事件。"
    },
    "priority": {
      "type": "integer",
      "enum": [4, 5],
      "description": "正常状态使用 4，严重失败或需要立即干预时使用 5。"
    },
    "tags": {
      "type": "array",
      "items": {"type": "string"},
      "description": "可选附加 ntfy 标签。"
    }
  },
  "required": ["emoji", "title", "message", "priority"],
  "additionalProperties": false
}
```

Codex 通过 MCP `tools/list` 返回的 `inputSchema` 得知 `emoji` 是必填参数及其结构用途。Schema 不提供候选集合或场景映射，具体 emoji 由模型根据当前通知内容自行决定；服务端不维护 emoji 字典，也不在 MCP Server `instructions` 或 AGENTS.md 中重复规定选择规则。

环内调用示例：

```json
{
  "emoji": "✅",
  "title": "设计方案已更新",
  "message": "统一消息格式和环外故障模板已经写入设计文档。",
  "priority": 4
}
```

Tool annotations：

- `readOnlyHint = false`
- `destructiveHint = false`
- `idempotentHint = false`
- `openWorldHint = true`

Codex 侧应将该 Tool 配置为自动批准，以支持任务进度、Git 提交和停止前的无人值守通知；只启用 `ntfy_send`，避免无关工具进入上下文。

## 8. 发送行为与返回结果

所有入口产生的发送请求都进入 `agent-notifierd` 内的有界 FIFO 待发送消息池，包括 MCP、CLI 和模型链路监测器自身产生的通知。消息池不跨会话合并、不丢弃语义相似的消息；单一 Worker 按入队顺序串行发送，避免多个 Codex 会话同时调用 `ntfy publish` 触发 ntfy.sh 限流。

消息池第一版只保证进程存活期间的排队和限流，不做磁盘持久化。提交方等待本次发送的最终结果；守护进程不可用或等待超时时返回明确失败，不绕过消息池直接发送，以免破坏全局串行约束。

发送实现使用无 Shell 的参数数组调用，禁止拼接 Shell 命令：

```text
/usr/bin/ntfy publish
  --title "EMOJI TITLE"
  --priority PRIORITY
  --tags TAGS
  https://ntfy.sh/TOPIC
  MESSAGE
```

批量发送规则：

- 一个发送请求及其解析出的全部最终话题构成一个队列任务。
- 按最终话题顺序逐个发送。
- 单个话题失败后继续发送其余话题，不采用 fail-fast。
- 收集每个话题的返回码、服务端消息 ID和错误摘要。
- 不把完整环境变量、认证信息或无界 stdout/stderr 返回给模型。
- 不仅检查 `ntfy` 进程退出码，还必须解析 JSON 输出；存在服务端 `code`、`http` 或 `error` 时按失败处理，即使进程退出码为 `0`。
- HTTP `429` 和瞬时 `5xx` 使用有界指数退避重试；重试期间保留当前 Worker 的发送顺序，不允许后续消息越过。

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

MCP Tool 只提供“发送通知”的能力，不负责识别任务阶段、Git 提交或 Agent 何时停止。这些触发时机属于 Agent 策略，由全局 `AGENTS.md` 或 `AGENTS.override.md` 中的稳定规则块负责。Skill 可以细化某类任务的小点，但不得取消全局规则要求的通知。

### 9.1 长程任务进度

以下任务视为长程任务：

- Agent 判断需要完成两个或以上有意义、可独立验证小点的复杂普通任务。
- 用户显式创建或要求持续执行的 goal 任务，不论它是否跨轮次继续。

Agent 应在开始执行时识别小点，每完成一个小点并获得相应验证后，在开始下一小点前调用一次 `ntfy_send`。通知应使用优先级 `4`，简要说明已完成内容、验证结果和下一步。

小点必须是用户能够理解的任务进展，不得把单个 Shell 命令、Tool Call、文件读取或局部编辑拆成伪小点刷屏。也不得等到整个任务结束后再成批补发进度通知。显式 goal 跨轮次继续时，已完成的小点不重复通知。

### 9.2 Git 提交通知

Agent 每次成功执行 `git commit` 后都必须立即调用一次 `ntfy_send`，包括使用 `--amend` 产生新提交的情况。通知使用优先级 `4`，正文至少包含短提交哈希和提交主题，不包含完整 diff、凭据或其他敏感内容。

每个成功提交至少对应一次通知，不得将多个提交合并成一条延后发送的汇总。如果该提交恰好完成一个长程小点，一条同时明确包含进度、验证结果、下一步和提交信息的通知可同时满足两项规则，以避免连续重复推送。`git commit` 失败时不伪造提交成功通知；若该失败导致任务阻塞，按交还规则通知。

### 9.3 任务交还与合并规则

除用户硬性打断外，在任何正常交还控制权、任务完成、任务阻塞、计划决策或命令完成前，Agent 仍必须调用 `ntfy_send`。最后一个小点或最后一次提交与整体任务完成重合时，可以使用一条同时涵盖所有状态的通知，不必为同一时点重复推送。

建议状态映射：

- 小点完成、提交成功、正常完成、等待输入、普通暂停或非致命异常：优先级 `4`。
- 严重失败、需要立即人工干预或不可恢复阻塞：优先级 `5`。
- emoji 不与优先级绑定，由模型根据通知内容自行选择。
- 通知失败不得掩盖原任务结果，也不得为了重发提交通知而重复执行 `git commit`；Agent 应继续任务并在最终答复中说明通知失败。

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

### 10.1 环外固定模板

环外通知不让模型生成内容。故障分类器根据 OTel 事件和清洗后的字段选择固定模板，再构造与 MCP 完全相同的 `Notification` 对象：

| 内部事件 | 判断条件 | emoji | 标题 | 优先级 |
|---|---|---|---|---:|
| `authentication_failed` | HTTP `401` 或 `403` | 🔐 | `模型认证失败` | 5 |
| `rate_limited` | HTTP `429`，宽限期后仍未恢复 | ⏳ | `模型服务限流` | 5 |
| `server_unavailable` | HTTP `5xx`，宽限期后仍未恢复 | 🚨 | `模型服务异常` | 5 |
| `connection_failed` | DNS、TCP、TLS 或无 HTTP 状态的连接失败 | 📡 | `模型服务连接失败` | 5 |
| `response_stream_disconnected` | SSE 或 WebSocket 响应流失败 | 🔌 | `模型响应流中断` | 5 |
| `transport_failure` | 无法归入以上类别的最终传输错误 | ⚠️ | `模型通信异常` | 5 |

分类按表格从上到下匹配，避免一个事件同时命中多个模板。正文按以下纯文本结构生成，只输出实际存在的字段：

```text
模型：{model}
通道：{transport}
状态：{status_summary}
尝试：{attempt}
错误：{sanitized_error}
```

动态字段必须截断和清洗；不得包含提示词、模型输出、认证头、Token 或完整原始载荷。环外模板不添加会话 ID 或项目名，也不跨会话合并消息。

处理规则：

1. 按会话 ID 和传输类型维护待确认故障。
2. 失败事件刷新待确认故障及最后错误摘要。
3. 同一会话和传输随后出现成功事件时清除待确认故障。
4. 待确认故障超过 `failure_grace_seconds` 后发送一次优先级 `5` 通知。
5. 消息只包含模型、传输类型、HTTP 状态、尝试次数和经过截断/清洗的错误摘要，不包含用户提示词、模型输出或认证信息。
6. 相同会话内的相同故障指纹在 `dedupe_window_seconds` 内抑制重复通知；不同会话之间不去重、不聚合。
7. 只保留仍在宽限期内的故障和尚未过期的去重记录；恢复、告警处理完成或窗口过期后及时清理，不永久保存历史会话状态。

这是一种可靠的“最终未恢复”近似判断，而不是对 Codex 内部重试状态机的复制。若后续证据表明 OTel 事件不足，再考虑将 App Server 适配器作为可选增强；第一版不实现。

当前先保留“会话 ID + 传输类型”的状态键。理论上，同一轮请求中的 API、SSE 和 WebSocket 事件可能产生重复告警，或不同轮次的事件可能相互影响；在没有真实故障样本前不预先引入 `turn.id` 和更复杂的关联状态机。实现时记录必要的诊断字段，若实际出现误清除或重复告警，再根据真实 OTLP 事件序列调整。

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
- 待发送消息池必须有固定容量；容量耗尽时返回明确的队列已满错误，不能无限占用内存。
- 第一版不实现用户名、密码、Token、附件、邮件转发和延迟发送，遵循 YAGNI。

## 12. 测试方案

### 12.1 单元测试

- 话题与组名称跨类型重复时拒绝配置。
- 一层组、多层组、重复引用按首次出现顺序去重。
- 直接循环、自循环和多节点循环均能报告完整路径。
- 空组和嵌套后为空的组解析为零话题。
- 缺失成员、缺失默认目标、非法优先级和未知字段均报错。
- CLI 修改配置失败时原文件保持不变。
- 环内、环外和 CLI 输入均规范化为相同 `Notification` 结构。
- MCP 缺少 emoji、标题、正文或优先级时拒绝调用。
- 空 emoji、非 `4/5` 优先级以及超过 UTF-8 字节限制的字段均被拒绝。
- emoji 不经过字典映射，最终标题只包含一个 emoji 前缀。
- 附加 tags 按首次出现顺序去重，不自动加入主 emoji。

### 12.2 发送测试

- 使用假的 `ntfy` 可执行文件验证参数数组，不访问真实网络。
- 单话题成功、全部成功、部分失败、全部失败和超时结果正确。
- 标题缺失、多个标签、中文消息和特殊字符不会产生 Shell 注入或错误拆词。
- 空组不启动 `ntfy` 子进程。
- 多个 MCP/CLI 并发提交时，消息全部进入同一 FIFO 队列并按顺序发送，不跨会话合并。
- `ntfy` 退出码为 `0` 但 JSON 包含 HTTP `429` 时仍判定失败，并执行有界退避重试。
- 消息池容量耗尽和等待超时均返回明确错误。

### 12.3 MCP 协议测试

- `initialize` 返回正确服务信息和 Tool capability。
- `tools/list` 只暴露 `ntfy_send`。
- `tools/call` 参数验证与 CLI 业务规则一致。
- stdout 只输出 MCP JSON-RPC，诊断日志只写 stderr。
- Tool 返回内容有界，不泄露环境变量和配置敏感值。
- `doctor` 仅检查起止标记之间的内容，不得将规则块外的文字误认为通知策略。
- 只包含旧版交还通知语义、缺少长程小点或 `git commit` 语义的规则块无法通过 `doctor`。

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
- 去重窗口内同一会话的相同错误被抑制；不同会话即使错误相同也分别通知。
- 已恢复、已处理和已过期状态会被清理，历史会话不会永久驻留内存。
- 原始事件中的提示词、输出、Token 和无界错误正文不会进入 ntfy 消息。
- HTTP `401/403/429/5xx`、连接失败、SSE/WebSocket 中断和未知传输错误均选择正确的固定模板。
- 同一 OTel 事件最多选择一个环外模板，动态字段缺失时正文不产生空标签行。

### 12.6 部署验收场景

- 在不同的受支持 Linux/WSL2 环境中，部署 Agent 能先探测差异并选择适合本机的安装方式，不依赖固定包管理器或绝对路径。
- 对同一目标状态连续部署两次，配置块、MCP 条目、systemd 服务和全局 Agent 指令均不重复。
- 预置包含其他字段的 Codex 配置和全局 Agent 指令，部署后无关内容保持不变并生成可识别的备份。
- 预置无法无损合并的 OTel 配置时明确停止，不覆盖原配置，也不伪造部署成功。
- 在缺少 ntfy 话题、不支持的平台、`systemd --user` 不可用和系统依赖缺失时返回可操作诊断。
- 部署完成后的 `agent-notifier doctor --json` 能逐项报告 CLI、配置、daemon、MCP、OTel 和通知规则状态。
- 从任意目录启动新 Codex 会话时都能发现 `agent-notifier` MCP 和全局通知规则，不依赖原克隆目录。
- 在新 Codex 会话中执行包含多个可验证小点的长程任务，每个小点完成时都能收到一条及时通知，且不会按单条命令刷屏。
- 在专用测试仓库中连续执行两次 `git commit`，每次成功后都能收到包含对应短哈希和提交主题的通知，不合并成延后汇总。

## 13. 完成标准

- 用户可以通过 CLI 或直接编辑 TOML 管理话题和递归话题组。
- 配置能够确定性检测重名、缺失引用和循环。
- CLI 与 MCP 共用同一业务实现，不复制解析或发送逻辑。
- CLI、MCP 和故障监测通知共用一个有界 FIFO 待发送消息池，所有通知保持独立并串行发送。
- 环内和环外通知使用同一个 `Notification` 模型及同一套校验、渲染和发送逻辑。
- Agent 只需了解单个 `ntfy_send` Tool。
- Agent 能在任务交还、长程小点完成和每次成功 `git commit` 后提供带语义的环内通知；后台守护进程仅兜底模型 API 与响应流故障。
- 单话题、话题组和空话题组在同一接口下行为明确。
- MCP Server 随 Codex 生命周期运行；`agent-notifierd` 作为独立 `systemd --user` 服务运行。
- 用户只需把 Git 地址和必要的 ntfy 话题交给本机 Agent；Agent 可依据 README 的目标状态和约束，自行适配本机环境并完成部署与验收。
- README 最前方提供一句话安装方式、CLI 配置入口和配置文件位置，并明确当前会话无法热加载新 MCP 的重启边界。
- 部署过程幂等、保留无关用户配置、为被修改文件创建备份，除系统包安装外不需要 root 权限。
- 不依赖 Codex PID、包装命令、心跳、App Server 代理或 transcript 解析。
- 本机 `ntfy.service` 保持禁用也能正常发送到 `ntfy.sh`。
- 自动化测试不依赖公网；真实 ntfy.sh 仅用于最终人工验收。
