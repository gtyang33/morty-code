# Claude Code Message Normalizer 二次深挖

## 结论

Claude Code 的 `normalizeMessagesForAPI()` 不是简单的格式转换，而是长会话恢复层。它承认 transcript 在这些情况下会变脏：

- streaming 被中断，assistant 的 thinking/text/tool_use 被拆成多个片段。
- compact 或 resume 从半个工具调用中间开始。
- 历史里出现重复 tool_use id 或孤儿 tool_result。
- attachment / system-reminder 被合并到 user turn 后，破坏 tool_result 的 API 约束。
- provider/beta 能力变化后，旧 transcript 里残留当前 provider 不支持的 block。

这个模块的价值是让会话尽量继续，而不是让一次坏历史永久 400。

## Claude Code 做得好的地方

### 1. API 前最后防线

Claude Code 不信任 transcript 原样合法。它会在 API 前执行：

- attachment materialize。
- adjacent user merge。
- assistant 同 `message.id` streaming chunk 合并。
- tool_result hoist。
- thinking-only orphan 过滤。
- trailing thinking 过滤。
- whitespace-only assistant 过滤。
- non-final empty assistant 补占位。
- tool_use/tool_result 双向 pairing 修复。

这保证了 resume、compact、retry 后仍能构造出 API 可接受的消息。

### 2. Pairing 修复是双向的

Claude Code 的 `ensureToolResultPairing()` 同时处理：

- assistant 有 tool_use，但后面没有 tool_result：插入 synthetic error tool_result。
- user 有 tool_result，但前面没有对应 tool_use：剥离 orphan tool_result。
- 重复 tool_use id：只保留第一次。
- 重复 tool_result id：只保留第一次。

这点很关键。只补 missing，不删 orphan，仍然会被 API 拒绝。

### 3. 它把失败修复写成可观测事件

源码里每个修复点都会 log event / log error。这样坏 transcript 不会静默消失，后续能追踪是谁制造了不合法状态。

## Claude Code 做得不好的地方

### 1. 多 pass 顺序脆弱

源码注释已经承认：先过滤 whitespace 还是先剥 trailing thinking，会造成不同结果。一个 pass 可能制造出另一个 pass 早已处理过的坏形态。

复刻时不应该继续把所有规则堆成一个黑盒函数，而应该显式分层：

1. block-level sanitize。
2. streaming assistant merge。
3. message-level cleanup。
4. tool pairing repair。
5. final API validator。

### 2. 修复和 provider 适配混在一起

Claude Code 的 normalizer 里同时有 transcript 修复、工具输入 canonicalize、feature gate、tool search beta、snip tag。这对产品系统很实用，但复刻时会让边界变脏。

`morty-code` 应保持 provider-neutral normalizer；OpenAI-compatible 的 wire 转换继续放在 provider adapter。

## morty-code 本轮实现

本轮把 `MessageNormalizer` 往 Claude Code 的防御性恢复模型推进：

- 添加 `NormalizationReport`，每轮记录修复类型和计数。
- 合并同一 assistant `payload.id` 的 streaming chunk。
- 过滤真正孤立的 thinking-only assistant message。
- 只剥离最后 assistant 的 trailing thinking，避免过早破坏可合并 chunk。
- 过滤 whitespace-only assistant message，并重新合并相邻 user。
- 为非最终 empty assistant 补中文注释说明的占位内容。
- 增强 tool_use/tool_result pairing：首条孤儿 tool_result 不再让 payload 以 assistant 开头；重复 tool_result 会被去重。
- 增加 final API validator，保证输出只包含 user/assistant，且不会有相邻同 role message。
- `QueryLoop` 将 normalizer 修复报告写入 metadata events，方便诊断。

## 暂不实现

- 不引入测试文件，符合当前“先整体实现”的约束。
- 不把 provider-specific block stripping 放进 normalizer；这仍由 `OpenAICompatibleModelClient` 处理。
- 不实现 snip/history tag，因为它会改变 prompt 字节并影响 cache 稳定性。
