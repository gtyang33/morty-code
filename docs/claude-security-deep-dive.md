# Claude Code 安全模块深挖

## Claude Code 的安全层次

Claude Code 的安全不是单点检查，而是多层叠加：

1. **权限模式**：`default`、`plan`、`acceptEdits`、`bypassPermissions`、`dontAsk` 等模式决定工具是否能直接执行。
2. **工具级校验**：Read/Edit/Write/Bash 都有自己的 `validateInput` 和 permission matcher。
3. **路径安全**：写入前检查危险文件、危险目录、配置目录、路径穿越、UNC path、symlink 解析。
4. **Bash 语义分析**：对 shell command 做 parse，识别 command substitution、redirection、危险命令、危险删除、只读命令。
5. **Sandbox**：在支持的平台上把 Bash 放进 filesystem/network sandbox，靠 OS/运行时拦截越界行为。
6. **审计与 transcript**：工具请求、拒绝、结果都进入 transcript，便于恢复和追踪。

## 关键优点

- **安全前置**：危险输入在执行前拒绝，而不是执行后补救。
- **deny 优先**：路径/权限 deny rule 先于 allow rule。
- **读后写**：文件写入要求先 read 且 mtime 未变，减少覆盖用户改动。
- **Bash 特殊处理**：Bash 不被当作普通字符串执行，而是拆命令、查 redirection、查 shell 绕过语法。
- **sandbox 与 permission 分离**：permission 是策略，sandbox 是执行隔离，两者互补。

## morty-code 本轮实现

新增 `morty_code.security`：

- `assert_safe_read_path`
- `assert_safe_write_path`
- `assert_safe_bash_command`
- `evaluate_tool_permission`
- `SecurityViolation`

接入本地工具：

- `read_file` 读取前确认路径仍在 workspace。
- `write_file` / `edit_file` 写入前拒绝 `.git`、`.claude`、`.ssh`、`.env*`、私钥名，以及 `.morty` 内部 transcript/task/tool-result 目录。
- `bash` 执行前拒绝 command substitution、process substitution、pipe-to-shell、`git reset --hard`、`git clean -f`、`git checkout .`、`rm -rf` 等危险模式。
- `bash` 默认拒绝 `sudo`、`su`、`chmod`、`chown`、`mkfs`、`mount`、`systemctl` 等高风险命令。
- 可通过环境变量 `MORTY_ALLOW_DANGEROUS_BASH=1` 临时绕过 bash 危险命令拦截。

## 权限决策深挖

Claude Code 的 `hasPermissionsToUseTool` 不是简单的工具白名单，它会综合：

- settings / cli / session / command 来源的 allow、deny、ask rules。
- 当前 permission mode，例如 `plan`、`acceptEdits`、`dontAsk`、`bypassPermissions`。
- 工具自己的 `checkPermissions`，例如 Bash prefix、文件路径、redirection、sandbox override。
- 是否能展示交互式 permission prompt。异步 agent / headless 场景不能弹窗时，ask 会走 hook 或自动拒绝。
- classifier / auto mode。部分 ask 决策可以交给分类器判断，但安全检查类拒绝不能被自动模式绕过。

本轮 morty-code 先实现最小统一策略层：

- `ToolRunner` 在调用任意 tool handler 前执行 `evaluate_tool_permission`。
- deny rule 优先于 allow rule。
- `plan` / `plan_mode` 阻止 `write_file`、`edit_file`、`bash`、`spawn_agent`。
- `acceptEdits` 对 `bash`、`spawn_agent` 返回 ask；由于当前没有交互 prompt，执行层按拒绝处理。
- `dontAsk` 只允许显式 allow 的工具。
- `bypassPermissions` 跳过 mode 约束，但仍尊重显式 deny rule，也不绕过工具内部安全检查。
- 可用 `MORTY_DENY_TOOLS=bash,spawn_agent` 增加 deny rule。
- 可用 `MORTY_ALLOW_TOOLS=read_file,list_files` 增加 allow rule。
- 可用 `MORTY_ASK_TOOLS=bash` 增加 ask rule；当前无交互 prompt，因此 ask 会被执行层拒绝。
- 可在 `.morty/permissions.json` 写项目共享权限，在 `.morty/permissions.local.json` 写本机权限：

```json
{
  "permissions": {
    "defaultMode": "acceptEdits",
    "allow": ["read_file", "list_files"],
    "ask": ["bash"],
    "deny": ["spawn_agent"]
  }
}
```

`.local` 文件在 shared 文件之后加载，环境变量和 `--permission-mode` 优先级最高。

## 后续深挖方向

按当前 morty-code 的完成度，后续安全模块建议按这个顺序继续：

1. **Bash scoped rule**：复刻 Claude Code 的 `Bash(prefix:*)` / subcommand 规则，让 `pytest`、`uv run`、`git status` 这类命令可精确放行，而不是粗暴放开整个 Bash。
2. **写入审批与 transcript 审计**：把权限拒绝、ask、allow 作为结构化事件写入 transcript，resume 后可追踪“为什么这个工具被拒绝”。
3. **交互式 permission prompt**：在 CLI 里支持本轮允许、会话允许、项目允许、本轮拒绝、永久拒绝。
4. **OS sandbox**：让 Bash 即使被允许，也只能写 workspace / tmp，并可限制网络。
5. **Claude settings 兼容**：逐步兼容 `.claude/settings.json` 的 permission schema，但避免让模型编辑 Claude/Morty 自己的安全配置。
6. **网络与 MCP 权限**：后续接 MCP / web 工具时，需要 domain-level allow/deny。

仍未实现：

- 交互式 permission prompt。
- OS 级 sandbox。
- Shell AST / tree-sitter 级精准解析。
- Claude settings 多来源兼容，包括 user/project/local/policy/cli 的完整优先级。
- 权限配置的写入命令和 schema 迁移。
- 网络域名 allow/deny。
- Bash prefix / path scoped permission rule。
- classifier / auto mode。
