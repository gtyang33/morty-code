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
- `SecurityViolation`

接入本地工具：

- `read_file` 读取前确认路径仍在 workspace。
- `write_file` / `edit_file` 写入前拒绝 `.git`、`.claude`、`.ssh`、`.env*`、私钥名，以及 `.morty` 内部 transcript/task/tool-result 目录。
- `bash` 执行前拒绝 command substitution、process substitution、pipe-to-shell、`git reset --hard`、`git clean -f`、`git checkout .`、`rm -rf` 等危险模式。
- `bash` 默认拒绝 `sudo`、`su`、`chmod`、`chown`、`mkfs`、`mount`、`systemctl` 等高风险命令。
- 可通过环境变量 `MORTY_ALLOW_DANGEROUS_BASH=1` 临时绕过 bash 危险命令拦截。

仍未实现：

- 交互式 permission prompt。
- OS 级 sandbox。
- Shell AST / tree-sitter 级精准解析。
- 按项目 settings 的 allow/deny rule。
- 网络域名 allow/deny。
