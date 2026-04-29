# morty-code

基于 Python 和 `uv` 的长会话 agent runtime 原型。

## 运行

```bash
uv run morty-code
uv run morty-code --once "读取 @README.md 并总结"
uv run morty-code --session .morty/sessions/<session-id>.jsonl
```

项目使用 `pyproject.toml` + `uv.lock` 管理环境，不需要 `pip install -r requirements.txt`。

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
