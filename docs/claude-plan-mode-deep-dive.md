# Claude Code Plan Mode 深挖

## 结论

Claude Code 的 plan mode 不是一个普通提示词开关，而是一套“先探索设计、再批准执行”的状态协议。

它包含四个关键部分：

- 进入 plan mode 后切换 permission mode，限制写入和危险操作。
- 给模型注入 plan mode attachment，持续提醒只能探索和写计划。
- 计划内容保存到独立 plan file，而不是只藏在聊天历史里。
- 退出 plan mode 通过 `ExitPlanMode`，把 plan 展示给用户审批，审批后再恢复执行权限。

这套机制的价值是把“想清楚再做”变成 runtime 约束，而不是靠模型自觉。

## Claude Code 做得好的地方

### 1. Plan 是文件，不只是消息

`utils/plans.ts` 会为 session 生成 plan slug，并把 plan 放在 plans directory。`ExitPlanMode` 读取 plan file，把它展示给用户。

这样做有几个好处：

- compact 后可以重注入 plan。
- resume 时可以恢复 plan slug。
- 用户审批看到的是确定文件内容。
- 执行阶段可以回看 approved plan。

### 2. Enter 和 Exit 都是工具协议

`EnterPlanMode` 负责进入计划阶段，`ExitPlanMode` 负责请求批准。它们不是简单 slash command，因为模型也能主动触发。

`ExitPlanMode` 还会处理：

- 不在 plan mode 时拒绝调用。
- 没有 plan file 时拒绝退出。
- 用户编辑后的 plan 回写磁盘。
- 退出后恢复进入 plan mode 前的 permission mode。
- 给下一轮注入 plan_mode_exit attachment。

### 3. Plan mode attachment 是节流提醒

Claude Code 不把 plan mode instruction 永久塞 system prompt，而是通过 attachment 周期性提醒。compact 后也会补 `plan_mode`，避免压缩丢状态。

## Claude Code 做得不好的地方

### 1. 状态分布太散

plan mode 状态跨越：

- bootstrap state。
- AppState permission context。
- tools。
- attachments。
- compact。
- REPL context clear。
- plan slug cache。

功能完整，但复刻时如果照搬会很快变成隐式状态网。

### 2. UI 审批和 runtime 协议耦合较深

`ExitPlanMode` 同时处理 UI permission request、teammate approval、mailbox、plan file、权限恢复。复刻时应先把核心 runtime 协议做清楚，再补 UI。

## morty-code 本轮实现

本轮实现最小 plan mode 闭环：

- 新增 `PlanStore`，按 session 管理 `.morty/plans/<session>.md`。
- `/plan [内容]` 进入 plan mode，创建 plan file，可选地直接写入初版计划。
- `/plan-save <内容>` 写计划文件。
- `/plan-show` 显示当前计划文件。
- `/auto` 从 plan mode 退出时要求 plan file 非空，并标记 approved plan。
- plan mode attachment 带 plan file path。
- plan mode exit attachment 带 approved plan 内容，下一轮会告诉模型可以开始执行。
- compact reinjection 会带当前 plan file 或 approved plan。

## 暂不实现

- 不实现 TUI 审批弹窗。
- 不实现模型主动调用的 `EnterPlanMode` / `ExitPlanMode` 工具。
- 不新增测试文件，继续使用脚本和 CLI 验证。
