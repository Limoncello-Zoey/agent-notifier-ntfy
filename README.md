# Agent Notifier for ntfy

面向 Codex Agent 的本地 ntfy 通知系统。项目已经实现可安装的 Python CLI、STDIO MCP Server、
有界串行发送队列，以及用于监测 Codex 模型链路故障的 OTLP/HTTP JSON 守护进程。

## 一句话交给 Agent 安装

直接把下面这句话发给运行在 Linux 或 WSL 中的 Codex Agent：

```text
请安装并配置这个项目：https://github.com/Limoncello-Zoey/agent-notifier-ntfy。克隆后严格遵循 README 中的“Agent 自适应部署指南”，先探测本机环境并自行决定合适的安装方式，持续执行到配置、服务启动和验收全部完成；仅在缺少 ntfy 话题等必要信息、需要我输入 sudo 密码或遇到无法安全自动解决的配置冲突时询问我。不要执行 curl | sh，不要删除或覆盖无关配置。
```

如果已经知道接收通知的 ntfy 话题，建议直接写进同一句话，避免安装中途询问：

```text
请安装并配置这个项目：https://github.com/Limoncello-Zoey/agent-notifier-ntfy，默认 ntfy 话题为 <TOPIC>。克隆后严格遵循 README 中的“Agent 自适应部署指南”，先探测本机环境并自行决定合适的安装方式，持续执行到配置、服务启动和验收全部完成；仅在需要我输入 sudo 密码或遇到无法安全自动解决的配置冲突时询问我。不要执行 curl | sh，不要删除或覆盖无关配置。
```

如果只希望在当前业务项目中启用通知，使用项目限定模式：

```text
请仅为当前受信任的 Git 项目安装并配置 https://github.com/Limoncello-Zoey/agent-notifier-ntfy，默认 ntfy 话题为 <TOPIC>。严格遵循 README 的“项目限定模式”：MCP 写入当前项目的 .codex/config.toml，通知规则写入项目根目录的 Agent 指令文件，不在用户级 Codex 配置或全局 Agent 指令中激活本工具，不启用 OTel 模型链路监测。持续执行到服务启动和项目级验收全部完成。
```

## 配置入口

部署完成后，所有日常配置都通过一个 CLI 完成：

```bash
agent-notifier config path
agent-notifier config show
agent-notifier config validate
agent-notifier topic list
agent-notifier group list
agent-notifier default show
```

默认配置文件位于：

```text
~/.config/agent-notifier/config.toml
```

可以直接编辑该 TOML 文件，也可以设置下面的环境变量覆盖位置：

```bash
export AGENT_NOTIFIER_CONFIG="/absolute/path/config.toml"
```

### 配置话题和话题组

话题使用一个本地名称映射到真实的 ntfy 话题。下面的示例创建两个话题，并将
`personal` 设为未显式指定发送目标时使用的默认目标：

```bash
agent-notifier topic set personal your_private_ntfy_topic
agent-notifier topic set builds your_build_ntfy_topic
agent-notifier default set personal
```

话题组由一个或多个已经存在的话题或话题组组成。发送到话题组时，通知会发送到组内
最终解析出的每个真实 ntfy 话题：

```bash
# 创建包含两个话题的组
agent-notifier group set team personal builds

# 组可以嵌套；这里 all 引用了已有的 team 组
agent-notifier group set all team

# 查看组的直接成员，以及递归解析、去重后的真实 ntfy 话题
agent-notifier group show team
agent-notifier group resolve all

# 将话题组设为默认发送目标
agent-notifier default set team
```

`group set NAME` 不提供成员时会创建空组。话题和话题组共享名称空间，不能重名；
组成员必须已经存在，并且组之间不能循环引用。CLI 会在写入前校验这些约束，校验失败
时不会修改配置。

常用的查看和删除命令如下：

```bash
agent-notifier topic list
agent-notifier topic remove NAME
agent-notifier group list
agent-notifier group remove NAME
agent-notifier default show
agent-notifier default clear
```

也可以直接编辑配置文件，等价的 TOML 配置示例如下：

```toml
[defaults]
target = "team"
priority = 4

[topics]
personal = "your_private_ntfy_topic"
builds = "your_build_ntfy_topic"

[groups]
team = ["personal", "builds"]
all = ["team"]
```

修改配置后执行：

```bash
agent-notifier config validate
systemctl --user restart agent-notifier.service
```

## 已实现接口

安装后可直接运行设计文档定义的全部公共入口：

```bash
agent-notifier config init
agent-notifier topic set personal your_private_ntfy_topic
agent-notifier default set personal
agent-notifier send --emoji "✅" --title "部署完成" --message "Agent Notifier 已可用"
agent-notifier daemon
agent-notifier mcp
agent-notifier doctor --json
agent-notifier doctor --scope project --project-root /absolute/project/path --json
```

`send` 和 MCP Tool 都只向本地守护进程提交请求，不会在守护进程不可用时绕过队列直连
ntfy。`daemon` 保持前台运行并把日志写到 stderr，由 `systemd --user` 负责常驻与重启。

源码开发与回归测试不需要运行中的 ntfy 服务：

```bash
python3 -m pip install -e '.[test]'
python3 -m pytest
```

Python 3.10 使用 `tomli` 读取 TOML；Python 3.11 及以上只使用标准库运行时依赖。

## Agent 自适应部署指南

本节是写给执行部署的 Agent 的提示与目标状态定义，不是一组适用于所有机器的固定命令。你必须根据当前机器的操作系统、架构、已有工具和用户配置自行决定部署细节。除非出现本节列出的暂停条件，否则应连续执行，不要把命令清单交还给用户手工完成。

### 1. 适用范围

第一版只支持：

- Linux，或已启用 systemd 的 WSL2；
- 本机 Codex CLI、Codex IDE 扩展或 ChatGPT 桌面版的本地 Codex host；
- Python 3、Git、`systemd --user` 和 Codex CLI 可用；
- 使用 `https://ntfy.sh` 或用户配置的兼容 ntfy 服务。

Windows 原生 Agent、Codex Web/Cloud、没有 systemd 用户实例的环境不属于第一版支持范围。检测到不支持环境时必须说明具体缺失项，不得伪造安装成功。

#### 1.1 选择部署作用域

本项目支持两种部署模式。用户明确说“仅当前项目”时选择项目限定模式；否则使用全局完整模式。已有配置与目标作用域冲突时不得静默删除或改变作用域，应向用户说明并请求决策。

| 组件 | 全局完整模式 | 项目限定模式 |
|---|---|---|
| CLI、应用配置、`agent-notifier.service` | 用户级 | 用户级，但守护进程本身只是被动的本地发送基础设施 |
| MCP 注册 | `~/.codex/config.toml` | `<project>/.codex/config.toml` |
| Agent 通知规则 | `$CODEX_HOME/AGENTS.md` 或 `AGENTS.override.md` | 项目根目录的 `AGENTS.md` 或 `AGENTS.override.md` |
| Agent 环内通知 | 所有项目 | 仅目标项目及其子目录 |
| Codex OTel 模型链路监测 | 用户级启用 | 禁用；项目级 `otel` 会被 Codex 忽略 |

Codex 只会为受信任项目加载 `.codex/config.toml`。项目级 MCP 是官方支持的配置方式，但 `otel` 只能放在用户级配置中；因此项目限定模式不包含模型链路故障监测。参见 [OpenAI Codex 配置作用域](https://developers.openai.com/zh-Hans/docs/config-file/config-reference)、[Codex MCP](https://developers.openai.com/zh-Hans/docs/extend/mcp) 和 [AGENTS.md 发现规则](https://developers.openai.com/zh-Hans/docs/agent-configuration/agents-md)。

### 2. 部署前探测

先用只读命令确认以下事实，不要询问用户可以从机器上直接获得的信息：

- 操作系统、发行版、架构、当前 Shell，以及是否位于 WSL；
- Python 版本和可用的用户级 Python 安装方式；
- `git`、`ntfy`、`codex`、`systemctl --user` 的可用性、版本和实际路径；
- `HOME`、可选的 `XDG_CONFIG_HOME`、`XDG_DATA_HOME` 与 `CODEX_HOME`；
- 目标部署作用域、Git 项目根目录及项目是否已被 Codex 信任；
- 现有 Agent Notifier 配置、用户级和项目级 Codex MCP 条目、OTel 配置、全局和项目 Agent 指令以及同名服务；
- 用户是否已经在请求中给出了 ntfy 服务地址、话题和必要认证信息。

基于这些事实选择本机最合适的安装方法。可以使用发行版包管理器、Python 虚拟环境或其他本机已有的标准工具，但不得假设某个特定包管理器、虚拟环境管理器或固定绝对路径一定存在。

#### 2.1 获取 ntfy CLI

若本机没有 `ntfy` CLI，部署 Agent 应前往 ntfy 官方仓库
[`binwiederhier/ntfy`](https://github.com/binwiederhier/ntfy)，阅读其最新 README、安装文档和
[Releases](https://github.com/binwiederhier/ntfy/releases)，再根据当前操作系统、发行版、架构和可用包管理器自行选择合适的版本与安装方式。本项目不接管或复制 ntfy 的具体安装指引，也不固定 ntfy 版本。

本项目只需要 `ntfy publish` 客户端；使用远程 `ntfy.sh` 时不需要启动本机 `ntfy.service`。

### 3. 安全与决策规则

1. 先完整阅读本 README 和 `docs/DESIGN.md`，再制定并执行部署方案。
2. 只从用户给出的 Git 地址克隆；不得使用 `curl | sh` 或执行其他未审查的远程脚本。
3. 使用普通用户权限安装应用、修改 Codex 配置并注册用户级服务；只有安装缺失的系统包时允许调用 `sudo`。
4. 不得索取、记录或代填 sudo 密码，应让用户直接在终端输入。
5. 修改现有配置前创建带时间戳的备份，并保留所有无关字段和内容。
6. 重复部署必须在选定作用域内收敛到同一状态：不得生成重复 MCP 条目、重复 OTel 配置、重复 Agent 指令或多个守护进程。
7. 应把应用安装到不依赖当前克隆目录的稳定用户级位置；具体路径根据本机约定决定。
8. 不得通过裸 `nohup`、后台 `Popen` 或 PID 文件派生守护进程；在当前支持范围内由 `systemd --user` 管理。
9. 只允许在以下情况暂停询问用户：缺少必要的 ntfy 目标或认证信息、需要用户输入 sudo 密码或确认项目信任、现有配置存在无法无损合并或作用域冲突，或当前平台不受支持。
10. 项目限定模式会修改目标仓库中的 `.codex/config.toml` 和 Agent 指令文件。必须保留安装前的 Git 状态，不得自动 `git add`、`git commit` 或更改仓库级 `.gitignore`；验收摘要中必须列出所有项目内变更，由用户决定是否纳入版本控制。

### 4. 必须达到的目标状态

两种模式都必须满足以下共同状态：

1. `agent-notifier` CLI 在普通用户环境中可直接调用，不依赖原克隆目录。
2. `ntfy` CLI 可用；应用配置已创建或无损合并，用户给出的话题已设置为默认目标。
3. 单实例 `agent-notifier.service` 已注册到用户级服务管理器、设置为自动启动且当前处于运行状态。
4. MCP Server 可以幂等确保守护进程已启动；守护进程启动失败时 MCP 仍能完成初始化并返回明确诊断。
5. 所有验收通过后，向用户输出部署作用域、安装方式、实际路径、服务状态、配置备份和后续重启要求的结构化摘要。

全局完整模式还必须满足：

1. `agent-notifier` MCP 唯一注册在用户级 Codex 配置中，只启用 `ntfy_send`，并将其 `approval_mode` 设为 `approve`。
2. Codex OTel 使用 OTLP/HTTP JSON 向 `http://127.0.0.1:4318/v1/logs` 导出，并保持 `log_user_prompt = false`。
3. Codex 实际读取的全局 Agent 指令文件中存在唯一通知规则块。

项目限定模式还必须满足：

1. 目标是明确的受信任 Git 项目；`agent-notifier` MCP 唯一注册在该项目根目录的 `.codex/config.toml` 中，并使用稳定已安装的 `agent-notifier mcp` 入口。
2. 项目 MCP 只启用 `ntfy_send`，并将其 `approval_mode` 设为 `approve`。
3. 项目根目录当前实际生效的 `AGENTS.override.md` 或 `AGENTS.md` 中存在唯一通知规则块，其他项目指令保持不变。
4. 项目 `.codex/config.toml` 中不得写入 `otel`；用户级 Codex 配置不得注册 `agent-notifier` MCP 或将 OTel 指向该守护进程，全局 Agent 指令中也不得留有 Agent Notifier 规则块。

全局或项目 Agent 指令都使用下面的稳定标记维护。重复部署时只更新标记之间的内容；开始标记、结束标记和规则块都只能出现一次：

```markdown
<!-- agent-notifier:rules:start -->
除用户硬性打断外，在任何正常交还控制权、任务完成、任务阻塞、计划决策或命令完成前，
调用 ntfy_send。对于需要两个或以上有意义、可独立验证小点的复杂普通任务，以及所有显式 goal 任务，
每完成并验证一个小点后、开始下一小点前，调用 ntfy_send 报告已完成内容、验证结果和下一步；
不要把单个命令、Tool Call、文件读取或局部编辑当作小点，也不要在任务结束后成批补发。
每次由 Agent 执行的 git commit 成功后（包括 --amend），立即调用 ntfy_send，正文包含短提交哈希和提交主题；
每个成功提交至少对应一次通知，不得合并多个提交后延迟汇总。如果提交恰好完成一个小点，或最后一个小点恰好完成整个任务，
可用一条明确涵盖所有相关状态的通知避免重复推送。小点完成、提交成功、正常完成、等待输入、普通暂停或非致命异常使用优先级 4；
严重失败、需要立即人工干预或不可恢复阻塞使用优先级 5。通知失败不得掩盖原任务结果，也不得为重发通知而重复执行 git commit，
但必须在最终答复中说明通知失败。
<!-- agent-notifier:rules:end -->
```

### 5. 验收要求

必须逐项验证，而不是仅凭安装命令的退出码宣告成功：

```bash
agent-notifier config validate
systemctl --user is-active agent-notifier.service

# 全局完整模式
agent-notifier doctor --scope global --json

# 项目限定模式
agent-notifier doctor --scope project --project-root /absolute/project/path --json
```

还必须验证：

- 本地健康检查端点可访问；
- MCP `tools/list` 只暴露 `ntfy_send`，其输入 Schema 正确；
- 在目标作用域中运行 `codex mcp get agent-notifier` 和 `codex mcp list`，确认条目唯一并已加载；
- 全局模式的 Codex OTel endpoint、协议和 `log_user_prompt = false` 已落盘；项目模式则确认项目配置没有 `otel` 且用户级无本工具的 OTel 路由；
- 通知规则写入 Codex 在目标作用域实际读取的 Agent 指令文件且只出现一次，规则同时覆盖任务交还、长程小点进度和 `git commit` 成功三类事件；
- 向用户配置的话题发送一条真实测试通知，并解析 ntfy JSON 响应确认存在 `event = "message"` 和消息 ID；仅检查进程退出码不算成功；
- 工作区和用户配置中不存在明文密码、Token 或意外产生的临时文件。

### 6. Codex 重启边界

Codex 在启动时读取 MCP 配置和 Agent 指令。因此部署 Agent 可以立即启动守护进程并完成配置，但正在执行部署的当前会话不会热加载新 Tool。项目限定模式的新会话还必须从目标项目或其子目录启动，并确认该项目已被 Codex 信任。

部署完成后，Agent 必须明确告诉用户：

- Codex CLI 需要退出并启动一个新会话；
- IDE 扩展需要 Restart extension；
- ChatGPT 桌面版需要在 MCP servers 页面选择 Restart；
- 新会话中使用 `/mcp` 确认 `agent-notifier` 已连接。

这不是安装失败，也不应要求用户重复部署。

全局完整模式中，同一 Codex host 上的 Codex CLI、IDE 扩展和 ChatGPT 桌面版共享 MCP 配置，不需要分别重复注册。项目限定模式只在客户端实际打开并信任目标项目时生效。

## 项目设计

完整架构、消息模型、环内 MCP 通知与环外 Codex OpenTelemetry 监测方案见 [`docs/DESIGN.md`](docs/DESIGN.md)。
