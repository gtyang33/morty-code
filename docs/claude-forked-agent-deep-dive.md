# Claude Code Forked Agent 深挖

## 结论

Claude Code 的 forked agent 不是普通“另起一个聊天”。它的关键是：

- 共享父线程 cache-safe 前缀，尽量命中同一段 prompt cache。
- clone mutable state，避免后台任务污染主线程。
- sidechain transcript 单独记录，主链 resume 默认不读 sidechain。
- 允许后台 fork 跳过最后 cache write，避免为一次性任务浪费 cache marker。
- 把 fork usage / cache hit / duration 写入 telemetry。

这让 session memory、post-turn summary、子 agent 等后台任务可以借用主线程上下文，但不破坏主线程。

## Claude Code 做得好的地方

### 1. CacheSafeParams 是显式契约

`forkedAgent.ts` 定义了 `CacheSafeParams`，包含 system prompt、user context、system context、tool context、fork context messages。

注释明确说 Anthropic cache key 依赖：

- system prompt。
- tools。
- model。
- messages prefix。
- thinking config。

这比“把父上下文传进去”更稳，因为它把 fork 和 cache 命中的关系写成了接口。

### 2. Mutable state 默认隔离

Claude Code 的 `createSubagentContext()` 默认 clone 或重建：

- read file state。
- content replacement state。
- nested memory trigger。
- dynamic skill trigger。
- discovered skills。
- abort controller。
- permission denial tracking。

其中 content replacement state 不是简单清空，而是 clone 父状态。原因是 fork 会处理父消息里的旧 tool_use_id；如果预算替换决策不同，wire prefix 就不同，cache 命中会下降。

### 3. Sidechain transcript 有独立生命周期

forked agent 会把初始消息和输出写入 sidechain transcript。主线程 resume 默认不把 sidechain 混进主链，避免后台摘要或子任务污染主对话。

## Claude Code 做得不好的地方

### 1. ToolUseContext 太大

`createSubagentContext()` 需要知道大量字段：UI callback、permission state、abort controller、agent id、query tracking、file state、memory trigger。这说明 `ToolUseContext` 已经变成了大状态总线。

复刻时应该保持小接口：先只显式处理当前 Python runtime 真正有的状态。

### 2. Fork 行为由很多布尔开关控制

`shareSetAppState`、`shareAbortController`、`skipTranscript`、`skipCacheWrite`、`maxOutputTokens` 都很有用，但组合多了以后容易出现 cache miss 或状态污染。

复刻时应该先只保留最小但明确的选项：

- fork label。
- max turns。
- skip transcript。
- skip cache write。

## morty-code 本轮实现

本轮实现的重点是把 fork 语义从“deepcopy 一下”提升到显式协议：

- `clone_tool_use_context_for_fork()` 明确 clone read_file_state、content replacement、memory/skill sets，并把父 prompt cache hashes 只放进 fork metadata 诊断。
- forked context 会写入 `app_state["fork"]`，标记 label、isolated、skip_cache_write。
- `ForkedAgentRunner.run_with_result()` 返回 messages + metadata_events + isolated context snapshot。
- `ForkedAgentRunner.run()` 保持旧接口，继续只返回消息列表。
- `QueryLoop.run()` 支持 per-run `max_iterations`，fork 不再靠截断消息数量伪装 max_turns。
- sidechain transcript 记录 fork start/metadata/finish/failure，并带 parent message count。

## 暂不实现

- 不实现真正并发 subagent 调度。
- 不新增测试文件，仍按当前“先整体实现”的要求只做脚本验证。
- 不实现 provider usage 精确累积；当前只透传 query loop metadata。
