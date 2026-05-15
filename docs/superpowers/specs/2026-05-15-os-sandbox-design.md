# OS Sandbox Design

## Goal

Add a real OS-level execution boundary for the `bash` tool so allowed commands still run inside a constrained filesystem sandbox.

## Scope

The first implementation targets Linux with `bubblewrap` (`bwrap`). It applies only to the `bash` tool. File tools keep their existing path guards and read-before-write behavior.

## Behavior

- `bash` executes through `bwrap` when available.
- The workspace root is writable inside the sandbox.
- `/tmp` is writable inside the sandbox.
- system paths needed to run common developer commands are mounted read-only.
- process and user namespaces are enabled through `bwrap`.
- network isolation is requested by default through `--unshare-net`.
- if the host blocks network namespace creation, the command still runs inside the filesystem sandbox and metadata reports `network_isolated: false`.
- missing `bwrap` fails closed and returns a tool error.
- `bypassPermissions` does not bypass the OS sandbox.
- `MORTY_DISABLE_OS_SANDBOX=1` disables sandboxing explicitly for local escape hatches.
- `MORTY_SANDBOX_NETWORK=1` keeps network access available inside the sandbox.

## Architecture

Add a small `morty_code.security.os_sandbox` module responsible for building the command argv used to invoke a shell under `bwrap`. Keep permission policy, dangerous Bash checks, and sandboxing as separate layers:

1. `ToolRunner` evaluates tool permissions.
2. `bash` validates dangerous shell forms with `assert_safe_bash_command`.
3. `bash` asks `os_sandbox` to wrap the shell invocation.
4. subprocess executes the wrapped argv without `create_subprocess_shell`.

## Testing

Unit tests cover deterministic argv construction, fail-closed behavior when sandboxing is required but `bwrap` is missing, the explicit disable flag, and network toggle behavior. Existing Bash tool tests continue to verify command output.
