# Claude Code Memory Rules for Morty Code

## 目标

本文基于当前 `claude-code-2.1.88/src` 的实现，提炼 Claude Code 的 memory 写入、读取和预算控制规则，并映射到 `morty-code` 的实现建议。

## Claude Code 的记忆分层

Claude Code 不是单一 memory 文件，而是三层机制：

1. **Auto memory / durable memory**
   - 路径由 `src/memdir/paths.ts` 计算：默认是 `<claude-config>/projects/<sanitized-project-root>/memory/`，也支持远程和可信 settings 覆盖。
   - 入口文件是 `MEMORY.md`，只作为索引；真正内容写入 topic `.md` 文件。
   - `MEMORY.md` 有热路径预算：最多 200 行，约 25KB，超限会截断并提示。

2. **Session memory**
   - 由 `src/services/SessionMemory/sessionMemory.ts` 管理。
   - 只记录当前会话连续性，服务于 compact 和恢复，不应该污染 durable memory。
   - 文件有固定模板，更新时必须保持 section header 和 italic instruction 不变。

3. **Relevant memories**
   - 由 `src/memdir/findRelevantMemories.ts` 和 `src/utils/attachments.ts` 负责。
   - 查询时扫描 topic 文件 frontmatter，根据用户输入选择最多 5 个相关 memory 注入。
   - 已通过 FileRead 或前序 relevant memory 注入过的路径会去重。

## 写入规则

### Durable memory 写什么

Claude Code 的 durable memory 类型在 `src/memdir/memoryTypes.ts` 中收敛为四类：

- `user`：用户角色、目标、偏好、背景知识。
- `feedback`：用户对工作方式的纠正或确认，包括为什么、怎么应用。
- `project`：当前项目中无法从代码或 git 历史直接推导的目标、背景、约束、事件。
- `reference`：外部系统或资料入口，例如 Linear、Grafana、Slack 等。

明确不保存：

- 代码模式、架构、文件路径、项目结构，因为这些可以通过读当前代码获得。
- git 历史、最近变更、谁改了什么，因为 git 是权威来源。
- 调试修复 recipe，因为修复应体现在代码和提交信息中。
- 已写在 `CLAUDE.md` 里的内容。
- 当前任务临时状态、进行中工作、一次性 conversation context。

### Durable memory 怎么写

Claude Code 的 durable memory 采用两步格式：

1. 为每个主题写一个独立 `.md` 文件，带 frontmatter：`name`、`description`、`type`。
2. 在同目录 `MEMORY.md` 中添加一行索引，索引短而具体，不能把正文写进 `MEMORY.md`。

写入时要按语义 topic 组织，优先更新已有文件，避免重复；如果发现旧 memory 错了或过时，要更新或删除。

### 写入触发

Claude Code 有两条 durable memory 写入路径：

- 主 agent 可以直接写 memory 文件。`extractMemories.ts` 会检测最近 assistant tool use 是否已经写入 auto-memory 路径；如果写了，后台提取会跳过，避免重复。
- 如果主 agent 没写，turn 结束后后台 `extract_memories` forked agent 会尝试提取。它有游标、并发互斥、节流、最多 5 turn 的上限，并且只允许 Read/Grep/Glob、只读 Bash、以及 auto-memory 目录内的 Edit/Write。

Session memory 的写入也是 forked agent，但触发条件不同：

- 只在 main thread。
- 依赖 feature gate 和 auto-compact。
- 初始 token 阈值达标后启用。
- 后续需要 token 增量阈值，并结合 tool call 数或自然对话断点。
- 手动提取可绕过阈值。

## 读取规则

### Durable memory 读取

Claude Code 默认把 `MEMORY.md` 索引放入上下文，让模型知道有哪些 memory，但不默认加载所有 topic 正文。

当用户输入可能需要 memory 时，会启动 relevant memory prefetch：

- auto memory 关闭或 feature gate 未开时不运行。
- 单词级短输入不运行。
- 会扫描 topic 文件 frontmatter，使用模型选择最多 5 个明确相关文件。
- 读取正文时有行数和字节上限，超限会注入截断提示。
- 已 surfacing 或已读过的 memory 不重复注入。

使用 memory 时必须当作“历史快照”，不能当作当前事实。若 memory 指向文件、函数或 flag，回答或建议前要读当前文件或 grep 验证；如果当前状态冲突，信当前状态，并更新或删除过时 memory。

### Session memory 读取

Session memory 主要在 compact 后恢复上下文，内容是当前会话工作笔记。Claude Code 的 prompt 明确要求它不替代 durable memory：只保存本会话有用的信息，长期有用的信息才进入 durable memory。

## Morty Code 当前实现状态

当前 `morty-code` 已经具备一个简化但方向正确的版本：

- `src/morty_code/memory/memory_extractor.py`
  - 返回结构化 `MemoryCandidate(text, target, topic, confidence, reason)`。
  - 把候选分为 `session` 和 `durable`。
  - 跳过普通回复、echo、runtime error、过短/过长内容、明显命令输出，并做同轮去重。

- `src/morty_code/memory/model_memory_extractor.py`
  - 使用当前模型从最近消息中提取结构化 JSON memory candidates。
  - 模型只负责总结和分类，Morty 仍负责校验、去重、路由和落盘。
  - provider 失败或返回非 JSON 时回退到 rule-based `MemoryExtractor`。

- `src/morty_code/runtime/query_engine.py`
  - `_write_memories()` 按 `candidate.target` 单目标写入，避免同一条内容同时进入 session 和 durable。
  - `_maybe_write_memories()` 已加入写入门槛：显式 `/memory` 或“记住/remember this”类请求优先；普通 turn 需要累计消息字符数超过阈值。

- `src/morty_code/memory/durable_memory.py`
  - 保持 `MEMORY.md` 索引和 topic 文件分离。
  - topic 文件写入 Claude Code 风格 frontmatter：`name`、`description`、`type`。
  - 重复写入同一条 memory 时跳过，避免 topic 文件和索引污染。
  - 对索引做行数和字节预算。

- `src/morty_code/prompt/prompt_builder.py`
  - 把 durable index 和 session memory 放在 `user_context`，避免污染 cache-safe system prompt 主体。

- `src/morty_code/memory/relevant_memory.py`
  - 已有基于关键词的 topic 文件检索和总预算限制，但还不是 Claude Code 那种 frontmatter manifest + 模型选择。

## Morty Code 实现建议

建议把 Claude Code 规则落成以下优先级：

1. **保持 session/durable 分层**
   - session：当前任务发现、临时决策、compact 后需要恢复的工作状态。
   - durable：跨会话仍有价值的用户偏好、项目非代码背景、外部引用、稳定约束。
   - 同一候选只能写一个层，不要双写。

2. **收紧 durable 分类**
   - 逐步把 Morty 的 `topic` 对齐到 `user`、`feedback`、`project`、`reference`。
   - 不保存代码结构、文件路径、git 历史、修复 recipe、当前任务进度。
   - 对中文显式请求也要支持：例如“记住”“以后记住”“下次按这个来”。

3. **保留写入门槛**
   - 普通 turn 不应每轮写 memory。
   - 显式 memory 请求应立即写。
   - 自动写入应依赖上下文规模或自然阶段边界，避免短回复污染 memory。

4. **改进 durable 文件格式**
   - topic 文件已改为 Claude Code 风格 frontmatter：`name`、`description`、`type`。
   - `MEMORY.md` 继续只做索引，一行一个 topic，保持短描述。
   - 后续 relevant memory 可以先扫描 frontmatter description，再决定是否读取正文。

5. **读取时遵守验证原则**
   - durable index 可以常驻 `user_context`。
   - topic 正文只在相关时注入。
   - memory 命中文件、函数、flag、当前状态时，使用前必须读当前文件或检索确认。

6. **后续再考虑 forked agent**
   - 当前已引入模型总结，但模型返回结构化候选，写入仍由 Morty 控制。
   - 等 query loop、工具权限和 prompt cache 更稳定后，再考虑引入后台 forked extraction；届时要照搬 Claude Code 的权限收敛：读工具 unrestricted，shell 只读，写工具只能写 memory 目录。

## 验证

已运行：

```bash
uv run pytest tests/test_memory_extractor.py tests/test_query_engine_memory.py -q
uv run pytest -q
```

结果：

- 记忆相关测试：9 passed。
- 全量 Morty 测试：64 passed，3 个已有 `datetime.utcnow()` deprecation warning。
