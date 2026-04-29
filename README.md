# morty-code

基于 Python 和 `uv` 的长会话 agent runtime 原型。

## 运行

```bash
uv run morty-code
uv run morty-code --once "读取 @README.md 并总结"
uv run morty-code --session .morty/sessions/<session-id>.jsonl
uv run morty-code --provider openai-compatible --model gpt-4.1-mini --once "hello"
uv run morty-code --enable-local-tools
```

项目使用 `pyproject.toml` + `uv.lock` 管理环境，不需要 `pip install -r requirements.txt`。
`openai-compatible` provider 使用标准库 HTTP 客户端，读取 `OPENAI_API_KEY` 和可选的 `OPENAI_BASE_URL`。

当前已实现：

- cache-safe prompt 构建
- query 前输入归一化
- 首轮 attachment 注入
- append-only transcript 落盘
- 最小 query loop
- compact / resume / forked agent 基础接口
- `@file`/`@directory` 附件读取与 API 前 materialize
- tool_use -> tool_result 的多轮回灌
- slash command 权限附件，例如 `/compact` 显式禁用工具
- durable/session memory 的预算控制与索引截断
- compact 后重新注入已读文件视图和 session memory
- resume/recovery 时清理孤儿 tool_result 与空 assistant 消息
- 大 tool_result 稳定替换，避免长会话 prompt 膨胀
- PromptBuilder 会把 durable memory index 与 session memory 注入 user_context
- forked agent 支持 sidechain transcript 落盘
- capability/skill discovery registry 基础结构
- OpenAI-compatible provider 基础实现，无额外 Python 依赖
- transcript 主链和 sidechain parent 分离，恢复默认只加载主链
- cwd 内只读本地工具：`read_file`、`list_dir`
- OpenAI-compatible provider 会发送本地工具 schema，并把 `tool_calls` 转成内部 `tool_use`
- 本地 slash command：`/status`、`/tools`、`/memory-index`
- 增量附件：`date_change`、`plan_mode`、`plan_mode_exit`、`hook_additional_context`
- 多模态输入会过滤无效 image block，并在 OpenAI-compatible provider 中转换为 `image_url`
- transcript metadata 会记录 `turn_start` / `turn_finish` 事件
- tool_result aggregate budget 会把超预算结果落盘为 `.morty/tool-results/<tool_use_id>.txt`，并记录 `content-replacement` metadata
- normalizer 会清理 `tool_reference`、smoosh `<system-reminder>` sibling，并修复 tool_use/tool_result pairing
