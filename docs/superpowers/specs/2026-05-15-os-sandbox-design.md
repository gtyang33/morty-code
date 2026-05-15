# OS Sandbox 设计

## 目标

为 `bash` 工具增加真正的 OS 级执行边界。即使命令已经通过权限策略，也必须在受限制的文件系统 sandbox 里运行。

## 范围

第一版只支持 Linux + `bubblewrap`（`bwrap`），只作用于 `bash` 工具。文件类工具继续使用现有 path guard 和 read-before-write 机制。

## 行为

- `bash` 默认通过 `bwrap` 执行。
- workspace root 在 sandbox 内可写。
- `/tmp` 在 sandbox 内可写。
- 常见开发命令依赖的系统路径以只读方式挂载。
- 通过 `bwrap` 启用 user / pid namespace。
- 默认请求 `--unshare-net` 做网络隔离。
- 如果宿主禁止创建 network namespace，命令仍在文件系统 sandbox 中执行，并在 metadata 中标记 `network_isolated: false`。
- 缺少 `bwrap` 时失败关闭，工具返回错误，不降级为裸 Bash。
- `bypassPermissions` 不绕过 OS sandbox。
- `MORTY_DISABLE_OS_SANDBOX=1` 可显式关闭 sandbox，作为本地逃生开关。
- `MORTY_SANDBOX_NETWORK=1` 允许 sandbox 内访问网络。

## 架构

新增小模块 `morty_code.security.os_sandbox`，专门负责构造通过 `bwrap` 启动 shell 的 argv。权限策略、危险 Bash 检查、OS sandbox 保持分层：

1. `ToolRunner` 先做工具权限判断。
2. `bash` 使用 `assert_safe_bash_command` 拦截危险 shell 形态。
3. `bash` 调用 `os_sandbox` 包装 shell 执行命令。
4. subprocess 使用 argv 直接执行，不再使用 `create_subprocess_shell`。

## 测试

单元测试覆盖稳定 argv 构造、sandbox 显式关闭、缺少 `bwrap` 时失败关闭、网络开关行为。已有 Bash 工具测试继续验证命令输出。
