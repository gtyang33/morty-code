# Claude Code 失败处理深挖

## 结论

Claude Code 的失败处理不是简单 `try/catch`。它把失败分成三类：

1. 可重试的 provider/API 失败：连接错误、408、409、429、5xx、529。
2. 可恢复的上下文失败：prompt too long、media too large、max output tokens。
3. 不可恢复但必须保持 transcript 合法的 runtime 失败：工具调用中断、fallback 失败、未知异常。

核心原则是：

- 前台主请求可以 retry，后台短任务尽量不要放大服务端压力。
- retry 过程会产出 `system api_error` 事件，让宿主知道还在等待。
- 如果流式响应中已经产生 `tool_use`，失败时必须补 synthetic `tool_result`，避免下一轮 API pairing 400。
- prompt-too-long 不直接报错，先尝试 context collapse / reactive compact，然后 retry。
- max output tokens 先提升输出上限 retry，再用 continuation message 恢复。
- fallback model retry 会清理失败尝试的 partial assistant/tool state，避免旧 tool_use_id 泄漏。

## 关键源码位置

- `src/services/api/withRetry.ts`
  - `withRetry()`：统一 retry/backoff。
  - `CannotRetryError`：包装不可继续 retry 的原始错误和 retry context。
  - `FallbackTriggeredError`：529 多次后触发模型 fallback。
  - `getRetryDelay()`：指数退避加 jitter，尊重 `retry-after`。
  - `shouldRetry()`：408/409/429/401/5xx/连接错误等分类。
- `src/query.ts`
  - 捕获 `FallbackTriggeredError` 后切换模型并清空 partial state。
  - 捕获普通错误后写 `tengu_query_error`，补齐 missing tool_result，再输出 assistant API error。
  - 对 prompt-too-long / media-size error 先 withheld，再尝试 compact/collapse retry。
  - 对 max_output_tokens 做 escalation retry。
- `src/utils/messages.ts`
  - `createAssistantAPIErrorMessage()`：把失败转成 assistant 消息。
  - `createSystemAPIErrorMessage()`：把 retry 等待转成 system metadata。

## Python 复刻目标

`morty-code` 当前没有流式 partial tool state，也没有 reactive compact 的完整体系。本轮实现最小闭环：

1. provider 错误变成 typed `ModelProviderError`，保留 status、detail、retry-after。
2. `QueryLoop` 对 retryable 错误做短 backoff retry，并写 `api-retry` metadata。
3. 如果 `MORTY_SEND_CACHE_CONTROL=1` 后 provider 返回 400，自动剥离 cache 字段重试一次，并写 `prompt-cache-disabled-for-retry` metadata。
4. cache plan / tool schema JSON 解析失败不再中断 turn，而是写 `prompt-cache-plan-failed` metadata 并降级为无工具 cache plan。
5. 最终失败不抛出到 CLI，而是生成 assistant API error message，并写 `query_failed` metadata。
6. `QueryEngine` 外层仍保留兜底，任何未捕获异常都会写 `turn_failed`，并返回 assistant error message。

这个范围优先保证长会话 transcript 不中断、下一轮仍可继续，而不是完整复刻 Claude Code 的 streaming recovery。
