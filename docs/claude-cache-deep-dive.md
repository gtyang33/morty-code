# Claude Code Cache 模块深挖

## 结论

Claude Code 的 cache 模块核心不是“把模型回答缓存在本地”，而是稳定服务端 prompt cache 的请求形状。

服务端 cache key 主要受这些输入影响：

- system prompt blocks
- tools schema
- model
- messages prefix
- thinking / effort / beta headers 等请求参数

因此 Claude Code 的工程重点是：

1. 用 `SYSTEM_PROMPT_DYNAMIC_BOUNDARY` 把静态 system prompt 和动态上下文分开。
2. 给 system、tools、最后一个消息块加 `cache_control`。
3. forked agent 复用父线程的 cache-safe 参数，但隔离 mutable runtime state。
4. 对 tool result replacement、tool schema、beta header、TTL 做冻结或粘滞，避免中途改字节。
5. 记录 cache read/create usage，并在 cache-critical 字段漂移时产生日志。

## 源码位置

- `src/constants/prompts.ts`
  - `SYSTEM_PROMPT_DYNAMIC_BOUNDARY`
  - 静态 prompt 在 boundary 前，动态 section 在 boundary 后。
- `src/utils/api.ts`
  - `splitSysPromptPrefix()`
  - `toolToAPISchema()` 的 schema cache 和 per-request overlay。
- `src/services/api/claude.ts`
  - `getCacheControl()`
  - `buildSystemPromptBlocks()`
  - `addCacheBreakpoints()`
- `src/services/api/promptCacheBreakDetection.ts`
  - 记录 system/tools/model/cache_control/beta 等漂移。
- `src/utils/forkedAgent.ts`
  - `CacheSafeParams`
  - forked agent 共享 cache-safe 前缀。
- `src/utils/fileStateCache.ts`
  - read file state 用 size-limited cache，避免长会话文件视图无限增长。

## Python 复刻范围

本轮在 `morty-code` 中实现的是请求形状层：

- `morty_code.cache.prompt_cache.PromptCachePlanner`
  - 切分 system prompt。
  - 生成 system prompt blocks。
  - 给最后一个消息块加 cache marker。
  - 给旧 tool_result 加 `cache_reference`。
  - 给最后一个 tool schema 加 cache marker。
- `PromptCacheBreakDetector`
  - 对 system、tools、cache_control、model、message prefix 计算稳定 hash。
  - 漂移时写 transcript metadata。
- `PromptCacheRuntimeState`
  - 保存会话级 cache hash 和 usage 累计。
- `QueryLoop`
  - 每次模型请求前生成 cache plan。
  - 默认只记录计划，不改变 OpenAI-compatible wire shape。
  - 设置 `MORTY_SEND_CACHE_CONTROL=1` 时透传 `cache_control`。
- `OpenAICompatibleModelClient`
  - 读取 provider usage。
  - 普通模式剥离 `cache_control` / `cache_reference`，避免 OpenAI 网关拒绝未知字段。

## 重要取舍

当前实现没有做本地响应缓存，也没有完整复刻 Anthropic Messages API。原因是 `morty-code` 当前 provider 是 OpenAI-compatible chat completions，默认网关不保证接受 Anthropic 的 `cache_control` 字段。

所以默认行为是：

- cache 计划和漂移检测总是运行。
- provider 请求默认保持 OpenAI-compatible。
- 只有明确设置 `MORTY_SEND_CACHE_CONTROL=1` 时才透传 cache 字段，适合接到支持这些字段的兼容网关。

这保留了 Claude Code 的 cache 工程语义，同时不破坏现有 CLI 可运行性。
