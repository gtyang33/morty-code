# Claude Code 架构优缺点深挖

## 结论

Claude Code 做得最好的地方，是它把 agent runtime 当成一个“长会话上下文操作系统”来设计，而不是一个简单的 chat loop。

它真正强的不是某个 prompt，而是这些工程能力组合在一起：

- prompt cache 稳定性是一等约束。
- attachment 作为动态上下文总线。
- transcript、runtime state、API message 三层分离。
- compact 是状态迁移，不是普通摘要。
- forked agent 共享 cache-safe 前缀但隔离 mutable state。
- message normalizer 会修复坏 transcript，保证 resume 后还能请求 API。
- retry / fallback / compact recovery 会把失败留在消息协议里，而不是让进程直接崩掉。

它做得不好的地方也来自同一个方向：能力太多、约束太隐式，最后大量关键逻辑堆在少数超大状态机里。比如：

- `src/utils/messages.ts` 约 5500 行。
- `src/utils/attachments.ts` 约 4000 行。
- `src/services/api/claude.ts` 约 3400 行。
- `src/query.ts` 约 1700 行。

这些文件内部有很多“顺序不能变”“marker 只能有一个”“这个 catch 必须静默”“这个 header 必须 latch”的隐式协议。系统很强，但维护成本高。

## 做得好的地方

### 1. Cache-first 的上下文工程

Claude Code 明确把 prompt cache 命中率当成架构目标。

证据：

- `SYSTEM_PROMPT_DYNAMIC_BOUNDARY` 把静态 system prompt 和动态内容分开。
- `splitSysPromptPrefix()` 会把 boundary 前内容标成 global/org cache scope。
- beta header、cache TTL、fast mode、cache editing header 会 latch，避免中途变化导致 cache key 抖动。
- tool schema 基础部分按 session cache，`cache_control` / `defer_loading` 作为 per-request overlay。

这点非常值得学。很多 agent 只是“每轮拼上下文”，Claude Code 是“每轮拼出来的字节要尽量稳定”。

### 2. Attachment-first 动态上下文

Claude Code 没有把所有变化都塞进 system prompt，而是把高波动内容做成 attachment：

- 文件和目录。
- relevant memories。
- plan mode reminder。
- date change。
- MCP/tool delta。
- queued command。
- hook additional context。

`attachments.ts` 里甚至专门避免 `Date.now()` 在 render 时改变 memory header，因为“saved 3 days ago”变成“saved 4 days ago”会 bust prompt cache。

这个设计很好：动态上下文显式、可审计、可在 compact 后重新注入。

### 3. Transcript / Runtime State / API Message 分层

Claude Code 不把 transcript 原样发 API。它在 API 前会走 `normalizeMessagesForAPI()`：

- 合并 adjacent user。
- hoist `tool_result`。
- strip / relocate `tool_reference`。
- smoosh `<system-reminder>` sibling。
- 修复 `tool_use` / `tool_result` pairing。
- 清理 orphaned thinking-only assistant。

这说明它承认长会话 transcript 会变脏，并把“恢复坏历史”设计成正式管线。这比假设历史永远合法更可靠。

### 4. 失败处理是协议，不只是异常

`withRetry()` 和 `query.ts` 展示了比较成熟的失败模型：

- 408/409/429/5xx/529 retry。
- retry 期间产出 `system api_error`。
- 529 多次可触发 fallback model。
- prompt-too-long 先 withheld，再尝试 collapse / reactive compact。
- max output tokens 可提升上限 retry。
- 失败前如果已经产生 `tool_use`，会补 synthetic `tool_result`，避免下一轮 pairing 400。

这点对长会话 agent 很关键：失败不是进程异常，而是会话状态的一部分。

### 5. Forked Agent 的 cache-safe 继承

`forkedAgent.ts` 的方向很正确：

- `CacheSafeParams` 继承 system prompt、user context、system context、tool context、fork context messages。
- 子 agent clone mutable state，不污染父线程。
- content replacement / file state 默认 clone，保证子线程看到的 prefix 决策和父线程一致。

这让后台总结、memory、side task 不必从零构建上下文，也不破坏父线程。

## 做得不好的地方

### 1. 超大文件承担太多协议

`messages.ts`、`attachments.ts`、`claude.ts`、`query.ts` 都是“多协议聚合点”。它们同时处理：

- API shape。
- prompt cache。
- feature flag。
- provider 差异。
- transcript recovery。
- UI/transcript side effect。
- compact / retry / hook 交互。

这种文件很难局部推理。一个小改动可能影响 cache bytes、VCR fixture、resume pairing、tool search、thinking signature。

### 2. Normalizer 多 pass 顺序脆弱

源码自己也承认：

> multi-pass normalizations are inherently fragile

例如先 strip thinking 再 filter whitespace，顺序反了就会留下 API 拒绝的空 assistant message。

这类逻辑长期会变成“靠注释维护的状态机”。更理想的设计是把它拆成：

- block-level sanitize。
- message-level pairing repair。
- alternation validator。
- final API schema validator。

每一步输出明确类型，而不是一个长 pipeline 互相补洞。

### 3. Attachment 生成时机偏晚

`attachments.ts` 里有直接注释：

> This is janky  
> TODO: Generate attachments when we create messages

现在 attachment 在 query 前集中计算，这让它必须处理 queued command、IDE、skills、memory、MCP、hook 等大量来源，也导致 timeout、silent catch、skip/drop 逻辑变多。

更好的方向是：输入生成 message 时就把可确定 attachment 绑定进去；query 前只做少量增量 discovery。

### 4. Feature flag 和 provider 差异侵入核心路径

Claude Code 里大量 `feature(...)`、GrowthBook gate、provider-specific beta header、first-party-only 字段混在核心 query/api path 中。

这对产品迭代很实用，但对开源复刻不友好：

- 本地阅读很难知道真实启用路径。
- 某些逻辑依赖“sticky latch”才不 bust cache。
- 部分错误处理依赖字符串匹配，比如 fast mode rejection 注释里承认 string matching fragile。

复刻时应该把这些做成显式 capability / provider adapter，而不是散落在主循环。

### 5. 太多隐式全局状态

cache latch、last cache-safe params、feature gate cache、header latch、prompt cache break detector 都依赖会话级全局状态。

这让“主线程稳定”更容易，但也让测试和 fork 行为更难理解。特别是多个 agent/source 并行时，需要非常小心区分：

- 哪些状态该继承。
- 哪些状态该 clone。
- 哪些状态该隔离。
- 哪些状态只是 telemetry。

## 对 morty-code 的建议

### 应该吸收

- 保持 `CacheSafeParams` 为一等结构。
- 继续强化 attachment-first，不把动态状态塞回 system prompt。
- normalizer 必须变成防御性修复管线。
- compact 必须保持 boundary + user summary + reinjection。
- provider failure 必须写 transcript metadata，而不是只抛异常。
- forked agent 默认 clone mutable state，但共享 cache-safe 前缀。

### 应该避免

- 不要让 `query_loop.py` 长成几千行。
- 不要把 provider beta 字段散落到业务逻辑里。
- 不要用 feature flag 改变核心协议结构，除非它经过 adapter 层。
- 不要让 attachment 在一个函数里处理所有来源。
- 不要把 normalizer 做成不可验证的多 pass 黑盒。

## 下一步最值得深挖的方向

我建议下一步深挖 `attachments.ts`。

原因：

1. 它是 Claude Code 上下文工程的中心，也是复杂性最高的模块之一。
2. 它连接 memory、IDE、MCP、skills、plan mode、date change、queued command。
3. 它的时机问题已经被源码注释标为 janky。
4. `morty-code` 目前 attachment 还比较薄，正适合在复杂性失控前设计好边界。

深挖目标应该是：

- 把 attachment 类型分层。
- 区分 input-time attachment 和 query-time delta attachment。
- 设计预算、去重、稳定渲染。
- 为 compact 后 reinjection 定义统一接口。
- 避免 attachment manager 变成新的 4000 行文件。
