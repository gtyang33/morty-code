# OS Sandbox 实现计划

> **给 agentic worker 的要求：** 按任务逐步实现时，需要使用 `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans`。步骤使用 checkbox（`- [ ]`）跟踪。

**目标：** 默认把 `bash` 工具命令放进 Linux `bwrap` OS sandbox 中执行。

**架构：** 新增聚焦的 `morty_code.security.os_sandbox` 模块，负责构造 sandbox argv 并返回配置 metadata。更新 Bash 工具，让它直接执行 argv，同时保留现有权限判断和危险命令检查。

**技术栈：** Python 3.12、asyncio subprocess、pytest、bubblewrap。

---

### 任务 1：Sandbox 命令构造器

**文件：**
- 新增：`src/morty_code/security/os_sandbox.py`
- 修改：`src/morty_code/security/__init__.py`
- 测试：`tests/test_os_sandbox.py`

- [ ] 编写失败测试，覆盖 `bwrap` argv 构造、显式关闭 sandbox、缺少 `bwrap` 时失败关闭、网络开关行为。
- [ ] 运行 `UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_os_sandbox.py -q`，确认测试因模块缺失而失败。
- [ ] 实现 `SandboxConfig`、`SandboxUnavailable`、`build_bash_argv`、`sandbox_metadata`。
- [ ] 从 `morty_code.security` 导出新符号。
- [ ] 重新运行 `UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_os_sandbox.py -q`，确认通过。

### 任务 2：接入 Bash 工具

**文件：**
- 修改：`src/morty_code/tools/builtin_tools.py`
- 测试：`tests/test_builtin_tools.py`

- [ ] 编写失败测试，证明 `bash` 返回 sandbox metadata 且仍能执行命令。
- [ ] 运行 `UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_builtin_tools.py -q`，确认新增测试失败。
- [ ] 使用 `build_bash_argv` 把 `asyncio.create_subprocess_shell` 替换为 `asyncio.create_subprocess_exec`。
- [ ] 保留 timeout、stdout/stderr 截断、cwd、env 行为。
- [ ] 重新运行 focused tests，确认通过。

### 任务 3：验证与文档

**文件：**
- 修改：`docs/claude-security-deep-dive.md`
- 修改：`docs/morty-code-current-state-and-gap-analysis.md`

- [ ] 更新文档，把 OS sandbox 从缺失能力调整为已实现 Linux/bwrap 第一版。
- [ ] 运行 `UV_CACHE_DIR=/tmp/uv-cache uv run pytest -q`。
- [ ] 运行 `UV_CACHE_DIR=/tmp/uv-cache uv run python -m compileall src`。
