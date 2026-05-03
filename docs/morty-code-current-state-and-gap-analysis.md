# morty-code 当前状态与差距分析

## 当前功能

`morty-code` 已经从最初的原型，演进成一个可恢复、可执行、可审计的长会话 agent runtime。

### 入口与交互

- `uv run morty-code`
- `uv run morty-code --once "..."`
- `uv run morty-code --session <transcript>`
- `uv run morty-code --input-format stream-json`
- `prompt_toolkit` REPL，支持历史、上下键、基础高亮、spinner

### 模型接入

- `EchoModelClient`
- `OpenAICompatibleModelClient`
- 支持 DeepSeek 这类 OpenAI-compatible API
- provider 失败会回灌到 transcript，不会直接中断主链路

### Transcript 与恢复

- JSONL transcript
- `turn_start` / `turn_finish` / `turn_failed`
- message normalizer
- conversation recovery
- session restore
- content replacement restore
- tool execution metadata event

### Prompt 与缓存

- prompt builder
- system / user / system_context 分层
- prompt cache planner
- cache break detector
- cache usage 记录
- compact 后重建 prompt prefix

### 附件与上下文

- `@file` / `@dir`
- relevant memories
- plan mode attachment
- command permission attachment
- compact reinjection attachment

### Compact 与记忆

- 自动 compact
- 手动 `/compact`
- summary 生成
- compact 后重建消息链
- session memory
- durable memory
- relevant memory 检索

### Plan Mode

- `/plan`
- `/plan-save`
- `/plan-show`
- `/auto`
- plan mode 下限制高风险写入

### Subagent

- `spawn_agent`
- agent definitions
- forked agent runtime state isolation
- subagent task registry
- background task persistence
- orphan task reaper
- task output 查询

### Tools

- `read_file`
- `list_dir`
- `glob_files`
- `grep_text`
- `file_info`
- `create_dir`
- `write_file`
- `edit_file`
- `multi_edit`
- `move_path`
- `bash`
- `todo_write`
- 工具 schema validation
- tool result budget
- tool execution structured metadata

### Security 与权限

- workspace root path guard
- sensitive path guard
- dangerous bash guard
- project permission settings
- permission modes
- Bash scoped rules
- harness permission handoff

### Harness

- `--input-format stream-json`
- stdin JSONL user/control_request
- stdout SDK-like events
- `initialize`
- `set_model`
- `set_permission_mode`
- `interrupt`
- `get_status`
- `can_use_tool`

## 完整执行流程

```mermaid
flowchart TD
    A[用户输入 / stdin JSONL] --> B{输入模式}

    B -->|REPL/text| C[InputDispatcher]
    B -->|stream-json| H[Harness stream-json]

    H --> H1{event type}
    H1 -->|control_request| H2[处理 initialize / set_model / set_permission_mode / get_status]
    H1 -->|user| C

    C --> D[UserInputProcessor]
    D --> D1[Slash command 处理]
    D --> D2[@file / @dir / memory / plan attachments]
    D --> E[Transcript append user + attachments]

    E --> F{是否需要 query}
    F -->|否| Z[返回 local/system 消息]
    F -->|是| G[AutoCompactDecider]

    G -->|需要 compact| G1[CompactAgent summarize]
    G1 --> G2[Rebuild messages + reinject attachments]
    G -->|不 compact| I[PromptBuilder]

    G2 --> I
    I --> I1[system_prompt]
    I --> I2[user_context]
    I --> I3[system_context + tool schemas]

    I3 --> J[PromptCachePlanner]
    J --> J1[cache plan / cache break detector]

    J1 --> K[QueryLoop model respond]
    K --> L[ModelClient]
    L -->|Echo/OpenAI-compatible| M[assistant message]

    M --> N{assistant contains tool_use?}
    N -->|否| Y[append assistant/result metadata]
    N -->|是| O[ToolRunner]

    O --> O1[find tool]
    O1 -->|missing| OX[tool_result error + unavailable event]
    O1 -->|found| O2[Tool schema validation]

    O2 -->|invalid| OY[tool_result InputValidationError + validation_error event]
    O2 -->|valid| P[Permission Policy]

    P --> P1{allow / ask / deny}
    P1 -->|deny| P2[tool_result error + blocked event]
    P1 -->|ask without harness| P2
    P1 -->|ask with harness| H3[emit can_use_tool control_request]
    H3 --> H4[wait control_response]
    H4 -->|deny| P2
    H4 -->|allow / updatedInput| Q[execute tool]
    P1 -->|allow| Q

    Q --> Q1[tool handler]
    Q1 -->|success| Q2[tool_result ok + success event]
    Q1 -->|exception| Q3[tool_result error + error event]

    Q2 --> R[Tool result budget]
    Q3 --> R
    OX --> R
    OY --> R
    P2 --> R

    R --> S[tool_result user meta message]
    S --> T[append tool messages to working messages]
    T --> U{max iterations reached?}
    U -->|否| K
    U -->|是| Y

    Y --> V[post-iteration attachments]
    V --> W[Transcript append messages + metadata]
    W --> X[CLI render / Harness SDK events]
```

## 和 Claude Code / Codex 的差距

### 还缺的关键能力

1. 真正的 OS sandbox
- 现在主要靠 permission policy 和 Bash 安全检查。
- 还缺文件系统 / 网络隔离 / sandbox override 的完整执行层。

2. 交互式 permission prompt
- `ask` 在普通 CLI 下仍然会拒绝。
- 只有 `stream-json` harness 可以外部审批。

3. Bash AST / 子命令级解析
- 现在支持 `Bash(prefix:*)`、wildcard、tool level policy。
- 还没有完整 shell AST 级别的 compound command 拆分和逐段权限判断。

4. Hooks
- Claude Code 有 PreToolUse / PostToolUse / PermissionRequest / Stop 等 hooks。
- morty-code 还没有 hook 插件协议。

5. MCP / 外部工具生态
- 还没有 MCP client/server transport。
- 也没有 remote tool discovery。

6. richer progress stream
- 已有 `tool_execution` metadata。
- 但还没有实时 progress、hook progress、tool summary streaming。

7. LSP / IDE 能力
- 暂时没有 diagnostics、symbol search、rename、go-to-definition 等 IDE 级工具。

8. 多模态文件支持
- 当前以 UTF-8 文本工具为主。
- 图片、PDF、notebook 等还不完整。

9. 更完整的 session 管理
- 已支持 resume。
- 但还缺 session list、tag、rename、fork by message 等管理能力。

10. Remote bridge
- 现在只有本地 `stream-json` harness。
- 还没有 WebSocket / direct connect / remote UI bridge。

### 和 Codex 的差距

1. 任务级工程化还不够深
- Codex 更偏长期 coding task orchestration。
- morty-code 现在更像一个可执行 CLI agent runtime，还没有同等级的任务调度和长链路恢复能力。

2. 反馈面还不够完整
- Codex 在工具、任务、审批、恢复、UI 之间的反馈链更完整。
- morty-code 现在已经有 transcript、harness、metadata，但还缺更统一的事件总线。

3. 外围生态不足
- Codex 的工具链、宿主整合、权限交互更成熟。
- morty-code 目前的核心仍集中在本地执行与恢复。

## 下一步最值得做的方向

1. Bash AST / compound command 拆分
2. 交互式 permission prompt
3. 进度流和 tool start/end 事件对外输出
4. OS sandbox
5. MCP / remote bridge
