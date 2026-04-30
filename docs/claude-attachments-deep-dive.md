# Claude Code Attachments 深挖

## 结论

Claude Code 的 attachment 模块是动态上下文总线。它把高波动信息从 system prompt 和 tool schema 中移走，用消息流里的 attachment 增量告知模型。

这套设计做得很好，但实现代价也很明显：`src/utils/attachments.ts` 接近 4000 行，承担了太多来源、预算、去重、权限、feature gate、cache 稳定性和 compact reinjection 的职责。源码里直接写了：

```text
This is janky
TODO: Generate attachments when we create messages
```

这说明 Claude Code 也意识到当前 attachment 生成时机偏晚，query 前集中计算导致函数越来越重。

## 做得好的地方

### 1. 把高波动上下文移出 system prompt

Claude Code 通过 attachment 承载：

- `@file` / `@directory`
- MCP resource
- queued command
- relevant memories
- skill discovery / skill listing
- plan mode / auto mode reminders
- date change
- changed files
- deferred tools delta
- agent listing delta
- MCP instructions delta
- hook additional context

这样 system prompt 和 tool schema 可以保持更稳定，减少 prompt cache bust。

### 2. attachment 是显式消息，不是隐藏状态

attachment 进入 transcript 后可以被恢复、compact、重新注入。它既是模型上下文，也是审计记录。

这比在 runtime state 里悄悄改 prompt 更可维护。

### 3. 重视稳定渲染

`relevant_memories` 的 header 会在 attachment 创建时预计算，避免 render 时调用 `Date.now()`。

原因很实际：`saved 3 days ago` 变成 `saved 4 days ago`，会让同一段上下文字节变化，导致 prompt cache miss。

### 4. delta attachment 减少工具 schema churn

`agent_listing_delta` 的注释很关键：agent list 曾经在 AgentTool description 中，导致 MCP async connect、插件 reload、权限模式变化时 tool schema 改动，造成大量 cache_creation。

把这类动态列表移到 attachment，可以让工具描述保持静态。

### 5. compact 后能重新注入状态

Claude Code 的 compact 会吃掉旧 attachment，因此 compact 逻辑会主动重发：

- read file state
- plan / mode reminders
- skill/tool/MCP delta
- agent state
- hook/session context

这说明 attachment 不只是输入增强，而是 compact 状态迁移的一部分。

## 做得不好的地方

### 1. 单函数收敛过多来源

`getAttachments()` 同时处理 input-time、query-time delta、thread-wide、main-thread-only、subagent、team/swarm、memory、skills、MCP、plan mode 等来源。

这些来源生命周期不同，却都在一个函数里聚合，导致大量条件分支。

### 2. 生成时机偏晚

源码注释承认 attachment 应该在创建 message 时生成。现在 query 前集中计算会带来几个问题：

- 用户输入已经入队，附件失败时语义不清。
- queued command 如果被无条件 drain 但 attachment 被禁用，会被静默丢弃，所以源码专门兜底。
- query 前需要 timeout，避免 attachment 阻塞提交。
- slash command prompt 和普通用户输入共用路径，必须增加 `skipSkillDiscovery` 这类补丁参数。

### 3. 去重和预算散落

相关 memory 的 surfaced path、changed file 的 readFileState、nested memory trigger、agent listing delta 都各自实现去重。

这些逻辑都合理，但没有统一接口，复刻时容易越写越散。

### 4. silent catch 很多

attachment 是增强上下文，不应轻易中断主对话，所以很多地方选择 catch 后返回空。

这是产品上合理的，但如果没有 metadata，调试会困难。Python 版应该保留“失败不阻塞”，但把失败写入 transcript metadata。

## morty-code 实现策略

本轮不追求复刻所有 attachment 类型，而是先把边界立起来：

1. `Attachment` 增加 `phase` 和 `stable_key`。
2. `AttachmentManager` 分成三类收集：
   - input-time：由用户输入直接决定，如 `@file`、relevant memory。
   - query-time delta：轮尾或下一轮增量，如 date change、plan mode、hook context、queued command。
   - reinjection：compact 后重注入 read file state、session memory、plan mode、skills、tool schema summary、content replacement state。
3. 统一预算：
   - 单 attachment content 最大字符数。
   - 单轮 attachment 数量上限。
   - 超预算时稳定截断，并标记 `truncated_by_budget`。
4. 统一去重：
   - 根据 `stable_key` 去重。
   - query-time delta 和 reinjection 默认跳过 transcript 中已经出现的 stable key。
   - input-time `@file` 不强去重，因为用户再次提及通常表示希望重新注入。
5. 失败不阻塞主流程，但返回可记录 metadata。

这个改造的目标是防止 `morty-code` 的 attachment manager 走向 Claude Code 当前的 4000 行聚合函数。
