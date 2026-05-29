# morty-code

`morty-code` 是一个基于 Python 和 `uv` 的长会话 Agent Runtime 原型，目标是提供接近 Claude Code / Codex 的本地工程协作体验：可恢复会话、工具调用、计划模式、子代理、MCP、skills、多模态输入和上下文压缩。

项目使用 `pyproject.toml` 管理环境，不需要 `pip install -r requirements.txt`。

## 快速开始

```bash
uv run morty-code
uv run morty-code --once "hello"
uv run morty-code --provider openai-compatible --model gpt-4.1-mini --once "总结 README"
uv run morty-code --enable-local-tools
```

在源码目录外操作其它项目时，推荐显式指定 `--project` 和 `--cwd`：

```bash
cd /path/to/workspace
UV_CACHE_DIR=/tmp/uv-cache uv run --project /home/transwarp/morty/claude-code-2.1.88/morty-code \
  morty-code --cwd /path/to/workspace \
  --provider openai-compatible \
  --model deepseek-chat \
  --enable-local-tools
```

`--cwd` 是 Morty 操作的目标工作区。`.morty/sessions`、`.morty/plans`、`.morty/tasks`、`.morty/memory`、本地工具权限、`@file` 附件解析都会跟随这个目录。

## 常用启动参数

```bash
morty-code [options]
```

| 参数 | 说明 |
| --- | --- |
| `--cwd <path>` | 指定目标工作区，默认是当前 shell 目录 |
| `--once <text>` | 提交一条消息后退出 |
| `-c`, `--continue` | 恢复当前 workspace 最近一次 `.morty/sessions` 会话 |
| `--session <jsonl>` | 恢复指定 transcript 文件 |
| `--input-format text\|stream-json` | 普通 REPL 或 stream-json harness |
| `--provider echo\|openai-compatible` | 模型 provider，默认 `echo` |
| `--model <name>` | 模型名 |
| `--base-url <url>` | OpenAI-compatible base URL，默认读取 `OPENAI_BASE_URL` |
| `--api-timeout <seconds>` | 单次模型请求超时时间 |
| `--enable-local-tools` | 启用本地文件、搜索、编辑、bash 等工具 |
| `--permission-mode <mode>` | 覆盖默认权限模式：`default`、`acceptEdits`、`bypassPermissions`、`dontAsk`、`plan` |
| `--decision-gate off\|auto\|always` | 复杂任务是否先生成多个方案供用户选择 |

OpenAI-compatible provider 读取：

```bash
OPENAI_API_KEY
OPENAI_BASE_URL
MORTY_API_TIMEOUT
OPENAI_TIMEOUT
LLM_TIMEOUT
```

## 当前支持的能力

### 会话与上下文

- 每个 workspace 下使用 `.morty/sessions/*.jsonl` 保存 append-only transcript。
- 支持 `-c` 恢复最近会话，或 `--session` 恢复指定会话。
- 支持 compact：`/compact` 会压缩历史，并在后续 query 前只保留最后一个 compact boundary 之后的上下文。
- compact 摘要会保留用户目标、助手动作、工具调用/结果和关键附件状态。
- 恢复时会清理孤儿 `tool_result` 与空 assistant 消息，减少坏 transcript 对后续请求的影响。
- tool result 超预算时会落盘到 `.morty/tool-results/<tool_use_id>.txt`，模型上下文中保留稳定摘要。

### 本地工具

通过 `--enable-local-tools` 启用，所有路径都限制在 `--cwd` workspace 内。

| 工具 | 用途 |
| --- | --- |
| `read_file` | 读取 UTF-8 文本文件，支持 offset/limit |
| `list_dir` | 列目录 |
| `glob_files` | 按 glob 找文件 |
| `grep_text` | 按正则搜索文本 |
| `file_info` | 查看文件/目录元信息 |
| `create_dir` | 创建目录 |
| `write_file` | 新建或全量覆盖文件，覆盖已有文件前必须先读 |
| `append_file` | 追加写文件，适合长文档分段写入 |
| `edit_file` | 精确替换文件文本，必须先读文件 |
| `multi_edit` | 一次执行多个精确替换 |
| `move_path` | 移动或重命名路径 |
| `bash` | 运行测试、构建、诊断命令 |
| `todo_write` | 维护当前任务 todo 列表 |

`bash` 的提示词明确要求：读文件、搜索、编辑、写文件优先使用专门工具，不用 `sed -i`、`python -c`、heredoc 等方式改文件。

### Slash Commands

REPL 中输入 `/` 会补全本地命令。

| 命令 | 说明 |
| --- | --- |
| `/help` | 显示可用命令 |
| `/status` | 显示模型、权限、工具、上下文、计划、memory、transcript 状态 |
| `/tools` | 显示当前可用工具 |
| `/skills` | 显示当前加载的 skills |
| `/mcp` | 显示 MCP server 和 tools；REPL 中 `/mcp` 会进入交互菜单 |
| `/tasks [task_id]` | 查看后台 subagent 任务 |
| `/memory-index` | 查看 durable memory 索引 |
| `/plan [request]` | 进入 plan mode |
| `/plan-show` | 查看当前计划文件 |
| `/auto` | 批准当前计划并退出 plan mode |
| `/compact` | 请求压缩对话 |
| `/memory` | 请求刷新会话记忆 |
| `/exit`, `/quit` | 退出 REPL |

### Plan Mode

Plan mode 用于复杂任务的“先计划，后实现”。

```text
/plan 实现一个游戏菜单管理页面
```

进入 plan mode 后：

- permission mode 会切到 `plan`；
- 计划文件路径位于 `.morty/plans/*.md`；
- 模型应先调研并给出计划，不应直接实现；
- 计划准备好后，CLI 会展示审批交互；
- 用户选择 approve 后，Morty 会把“批准，直接实现”回灌到主会话，并恢复原权限模式；
- 用户也可以使用 `/auto` 批准当前计划。

### 权限控制

Morty 支持工具权限模式和 allow/deny/ask 列表：

- CLI 参数：`--permission-mode`
- 环境变量：`MORTY_PERMISSION_MODE`
- 环境变量工具列表：
  - `MORTY_ALLOW_TOOLS`
  - `MORTY_DENY_TOOLS`
  - `MORTY_ASK_TOOLS`

运行时 `/status` 会展示当前权限来源和规则。

### MCP

支持 Claude 风格的 stdio MCP server 配置、后台加载和 REPL 菜单。

添加 MCP server：

```bash
uv run morty-code mcp add mysql_query -s user \
  -e MYSQL_HOST=127.0.0.1 \
  -e MYSQL_PORT=3306 \
  -e MYSQL_USER=root \
  -e MYSQL_PASS=secret \
  -e MYSQL_DB=hive_metastore \
  -- npx @benborla29/mcp-server-mysql
```

配置位置：

- user scope：`~/.morty/mcp.json`
- project scope：`<workspace>/.morty/mcp.json`

REPL 中：

```text
/mcp
/mcp mysql_query detail
/mcp mysql_query tools
/mcp mysql_query reconnect
/mcp mysql_query disable
/mcp mysql_query enable
```

Morty 启动时不会阻塞等待 MCP 连接完成；server 初始状态是 `pending`，后台连接成功后将 MCP tools 注入工具池。MCP tool 会包装成：

```text
mcp__<server_name>__<tool_name>
```

### Skills

支持 Claude/Morty 风格的 `SKILL.md`：

- 用户级：`~/.morty/skills/<skill-name>/SKILL.md`
- 项目级：`<workspace>/.morty/skills/<skill-name>/SKILL.md`
- Claude 兼容：`<workspace>/.claude/skills/<skill-name>/SKILL.md`

加载顺序是全局到项目，项目内同名 skill 会覆盖全局 skill。

`SKILL.md` 支持 frontmatter：

```markdown
---
description: "说明 skill 做什么"
when_to_use: "什么时候使用"
allowed-tools: read_file, grep_text
argument-hint: "<path or topic>"
model: gpt-4.1
effort: high
context: fork
agent: Explore
user-invocable: true
disable-model-invocation: false
---

# Skill 内容

使用 $ARGUMENTS 接收参数。
当前 skill 目录：${MORTY_SKILL_DIR}
当前 session：${MORTY_SESSION_ID}
```

模型可通过 `skill` 工具按需加载完整 skill 内容，避免所有 skill 常驻 prompt。`context: fork` 的 skill 会在 forked subagent 中执行。

### Subagent

启用工具池后，模型可用 `spawn_agent` 派发子代理任务：

- 支持同步子代理，返回结果和 output file；
- 支持 `run_in_background=true` 后台运行；
- 后台任务状态保存在 `.morty/tasks`；
- 子代理 transcript 保存在 `.morty/subagents`；
- 父 agent 会收到 `task-notification`；
- 可用 `task_output` 或 `/tasks` 查询后台任务；
- 支持 `send_message` 给后台子代理排队发送消息。

项目自定义 agent 放在：

```text
<workspace>/.morty/agents/*.md
```

### 多模态输入

当前支持图片输入：

- 直接输入图片路径：`./screenshot.png`
- Markdown 图片语法：`分析 ![截图](./screenshot.png)`
- data URL：`data:image/png;base64,...`
- REPL 粘贴文本中包含上述形式时自动转换为 `[Image #n]`
- Linux 下 `Ctrl+V` 可尝试从系统剪贴板读取图片，需要 `wl-paste` 或 `xclip`
- stream-json content blocks 支持 base64 image source

内部会把图片保存为结构化 content block：

```json
{
  "type": "image",
  "source": {
    "type": "base64",
    "media_type": "image/png",
    "data": "..."
  }
}
```

OpenAI-compatible provider 会转换为 Chat Completions 的 `image_url` data URL。注意：最终能否识别图片取决于所选模型和网关是否真正支持 vision。

### Memory

- session memory：`.morty/session_memory.md`
- durable memory：`.morty/memory`
- `/memory-index` 可查看 durable memory index
- MemoryExtractor 默认使用当前模型提取结构化候选；provider 失败或非 JSON 响应时回退到规则提取
- PromptBuilder 会把 durable memory index 与 session memory 注入 user context

### Prompt Cache 与调试

Morty 会记录 prompt cache 相关状态：

- system boundary
- message cache marker
- tool schema marker
- cache usage metadata

Provider 报错时可打开 prompt dump：

```bash
MORTY_DUMP_PROMPT_ON_ERROR=1 \
MORTY_PROMPT_DUMP_DIR=.morty/prompt-dumps \
UV_CACHE_DIR=/tmp/uv-cache uv run morty-code \
  --provider openai-compatible \
  --model deepseek-chat \
  --once "总结当前功能"
```

dump 文件会写入 `.morty/prompt-dumps`，包含当次实际发送给模型的 prompt、messages 和上下文。它可能包含源码片段、工具结果、memory 或用户输入，分析后按需删除。

## stream-json

```bash
uv run morty-code --input-format stream-json --provider openai-compatible --model gpt-4.1
```

支持用户事件中的文本和图片 content blocks，适合被其它进程集成。

## 工作区文件结构

```text
<workspace>/.morty/
  sessions/          # 会话 transcript
  plans/             # plan mode 计划文件
  tasks/             # 后台 subagent 任务状态和输出
  subagents/         # 子代理 transcript
  agents/            # 项目自定义 agents
  skills/            # 项目级 skills
  memory/            # durable memory
  tool-results/      # 超预算工具结果落盘
  mcp.json           # project-scope MCP 配置
  repl_history       # REPL 历史
```

## 测试

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run pytest -q
UV_CACHE_DIR=/tmp/uv-cache uv run python -m compileall src
```

针对多模态链路：

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run pytest \
  tests/test_multimodal_input.py \
  tests/test_clipboard_image.py \
  tests/test_clipboard_text.py \
  tests/test_stream_json_multimodal.py \
  -q
```

## 当前边界

- 默认 provider 是 `echo`，真实模型需要使用 `openai-compatible`。
- 本地工具默认不启用，需要显式传 `--enable-local-tools`。
- MCP 当前覆盖 stdio server、`tools/list` 和 `tools/call`。
- 图片输入链路已支持，但视觉理解能力取决于模型和 OpenAI-compatible 网关。
- `bash` 有安全策略限制；文件读写编辑应使用专门工具。
- 这是 runtime 原型，部分行为仍在快速迭代中。
