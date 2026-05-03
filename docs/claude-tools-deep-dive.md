# Claude Code Tools 模块深挖

## Claude Code 的工具结构

Claude Code 的工具不是简单函数列表，而是一套完整协议：

- Tool schema：用 zod 定义模型可见参数，并尽量把默认值、约束和使用建议写进 schema。
- prompt/description：工具说明会进入模型上下文，决定模型是否会正确选择工具。
- validateInput：在执行前检查路径、文件新鲜度、命令形态和参数合法性。
- permission：读、写、bash、MCP 等工具都有权限判定和用户确认层。
- call：执行真实动作。
- mapToolResult：把结构化结果压成模型容易消费的 tool_result。
- result budget：大结果会落盘或替换为稳定 placeholder，避免上下文爆炸。
- state tracking：Read 后记录文件 mtime，Edit/Write 前要求先读且文件未被外部修改。

核心工具分层：

1. 文件发现：Glob / Grep。
2. 文件读取：Read，支持 offset/limit、图片/PDF/notebook 和文件状态记录。
3. 文件修改：Edit / Write，要求读后写，避免覆盖用户外部修改。
4. 命令执行：Bash，包含权限、sandbox、超时、后台任务、输出压缩。
5. 任务状态：TodoWrite、TaskOutput、Agent。
6. 外部能力：WebFetch/WebSearch、MCP、LSP、IDE。

## 做得好的地方

- Read-before-write 规则非常关键，能显著降低覆盖用户改动的概率。
- Grep/Glob 作为专用工具，比让模型一上来跑 bash 更稳定，也更省上下文。
- Bash 的描述字段、超时、后台化、输出压缩都围绕“可解释、可恢复、可审计”设计。
- 工具结果始终回灌 transcript，不走旁路，resume/compact/subagent 才有统一数据源。

## morty-code 本轮实现

本轮把本地工具从只读原型扩展为能做实际工程任务的基础工具集：

- `read_file`：支持 offset/limit，并记录 `read_file_state`。
- `list_dir`：列目录。
- `glob_files`：按 glob 找文件，跳过 `.git`、`.venv`、`node_modules` 等噪声目录。
- `grep_text`：用 Python regex 搜索文件内容，支持 content/files/count 三种输出模式。
- `write_file`：创建或覆盖文件；覆盖已有文件前必须完整 read。
- `edit_file`：精确字符串替换；要求先完整 read，默认要求 old_string 唯一。
- `multi_edit`：一次应用多个精确字符串替换；同样要求先完整 read，逐条检查 old_string。
- `file_info`：读取文件/目录元数据。
- `create_dir`：创建目录，走写入安全检查。
- `move_path`：移动/重命名文件或目录，源和目标都走写入安全检查，目标必须不存在。
- `bash`：在 workspace root 下执行 shell 命令，带超时和 stdout/stderr 截断。
- `todo_write`：维护当前 session 的 todo list。
- ToolRunner 现在会记录结构化 `tool_execution` metadata event，包括 unavailable、permission、blocked、start、success、error。

## Tool Execution 实现细节深挖

Claude Code 的 `toolExecution.ts` 把一次工具调用拆成多个阶段：

1. 找工具：支持真实工具、MCP 工具、旧工具别名回退。
2. 参数校验：模型生成的参数先过 schema，失败会回灌 tool_result error。
3. 权限判断：先 deny / ask / mode，再进入工具自身 checkPermissions。
4. 用户阻塞态：permission prompt、hook、classifier 等阶段会产生 progress 或 tracing span。
5. 真正执行：执行前后记录 duration、telemetry、hook。
6. 结果映射：工具原始结果会转换成稳定 tool_result，并可做大结果落盘。
7. 失败处理：未知工具、权限拒绝、异常、取消都会变成 transcript 里的 tool_result。

morty-code 本轮先复刻第 3、5、7 点的“结构化轨迹”：

- 权限 allow/ask/deny 写入 metadata。
- 外部 harness 审批后的最终决策写入 metadata。
- 工具 start/success/error 写入 metadata。
- QueryLoop 每轮 tool_runner 后把这些事件排入 transcript metadata event。

仍未实现：

- 细粒度 permission prompt。
- Bash sandbox / 安全 AST 分析。
- 后台 Bash task。
- 图片/PDF/notebook 读取。
- LSP、MCP、Web 工具。
- 结构化 diff hunk 和 IDE 诊断。
- 删除/清理类工具。需要先补回收站或更细审批，否则破坏面过大。
