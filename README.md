# morty-code

基于 Python 和 `uv` 的长会话 agent runtime 原型。

当前第一阶段已实现：

- cache-safe prompt 构建
- query 前输入归一化
- 首轮 attachment 注入
- append-only transcript 落盘
- 最小 query loop
- compact / resume / forked agent 基础接口
