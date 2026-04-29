# Claude Code Compact 深挖

## 结论

Claude Code 的 compact 不是“把历史摘要一下”，而是一次上下文状态迁移。

compact 结果会被重建成固定顺序：

```text
compact boundary
summary messages
messagesToKeep
post-compact attachments
hook results
```

之后每次 query 都从最后一个 compact boundary 开始取消息。也就是说，compact boundary 不是普通日志，而是模型上下文的切片锚点。

## 源码位置

- `src/services/compact/compact.ts`
- `src/services/compact/sessionMemoryCompact.ts`
- `src/utils/messages.ts`
- `src/query.ts`

关键函数：

- `buildPostCompactMessages()`
- `annotateBoundaryWithPreservedSegment()`
- `createPostCompactFileAttachments()`
- `createPlanAttachmentIfNeeded()`
- `createSkillAttachmentIfNeeded()`
- `createPlanModeAttachmentIfNeeded()`
- `createAsyncAgentAttachmentsIfNeeded()`
- `createCompactBoundaryMessage()`
- `getMessagesAfterCompactBoundary()`

## Boundary 语义

`createCompactBoundaryMessage()` 创建 `system compact_boundary`，其中包含：

- trigger: manual / auto
- preTokens
- messagesSummarized
- logicalParentUuid

`getMessagesAfterCompactBoundary()` 从最后一个 boundary 开始切片。boundary 本身会被 `normalizeMessagesForAPI()` 过滤，不直接发给模型，但它决定后续哪些消息还属于有效上下文。

Python 版之前只有一个 system summary message。由于 normalizer 过滤 system message，这意味着 summary 实际不会进入模型上下文。正确做法是：

- system compact_boundary 只做边界和 metadata。
- user compact summary message 承载摘要文本，供模型继续使用。

## messagesToKeep

Claude Code compact 不一定丢掉全部旧消息。它会保留 retained tail，尤其是最近仍然有执行意义的消息。

重要约束：

- retained tail 不能包含旧 compact boundary，否则后续“最后一个 boundary”会选错。
- retained tail 不能破坏 `tool_use` / `tool_result` 配对。
- 如果 tail 从孤儿 `tool_result` 开始，resume/query 会出现 API pairing 错误。

因此 Python retained tail 应该做 basic pair-safe cleanup：

- 去掉旧 compact boundary。
- 如果开头 user message 只有 tool_result 且前面没有 assistant tool_use，丢掉或让 normalizer 修复。
- 尽量从最近 user/assistant 轮次边界开始保留。

## Post-compact reinjection

Claude Code compact 后会重新注入被摘要吃掉但当前任务仍需要的状态：

- 最近读过的文件内容，带 token/file budget。
- plan file。
- plan mode reminder。
- invoked skills。
- deferred tools delta。
- agent listing delta。
- MCP instructions delta。
- async agent task status。
- session start hook results。

Python 当前已有 read file state 和 session memory reinjection，但还缺：

- plan mode reminder。
- discovered skill names。
- command permissions / tool schemas 摘要。
- content replacement state 摘要。

这些应该作为 attachment 注入，而不是塞回 system prompt 主体。

## 和 tool result budget 的顺序

Claude Code 在 query 前先执行 aggregate tool result budget，再执行 snip / microcompact / compact 相关逻辑。原因是：

- tool result replacement 只按 `tool_use_id` 决策。
- compact 不需要理解完整 tool result 内容。
- replacement 记录写 transcript 后，resume 能恢复同样的模型可见字节。

Python 版已经实现 aggregate budget，compact 侧需要保证：

- compact boundary 之后仍然可恢复 replacement state。
- post-compact summary 不重新展开已经被 replacement 的大结果。

## Python 实现计划

本轮实现三个最小闭环：

1. `CompactAgent` 输出 system boundary + user summary，而不是 system summary。
2. `QueryEngine` 在 query 前只使用最后一个 compact boundary 之后的 messages。
3. retained tail 去掉旧 boundary，并对开头孤儿 tool_result 做保护。
4. post-compact reinjection 增加 plan_mode、discovered skills、tool schema summary、content replacement summary。
