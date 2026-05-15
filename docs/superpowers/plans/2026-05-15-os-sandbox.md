# OS Sandbox Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run `bash` tool commands inside a Linux `bwrap` OS sandbox by default.

**Architecture:** Add a focused `morty_code.security.os_sandbox` module that builds sandboxed argv and reports configuration. Update the Bash tool to execute argv directly, preserving existing permission and dangerous-command checks.

**Tech Stack:** Python 3.12, asyncio subprocess, pytest, bubblewrap.

---

### Task 1: Sandbox Command Builder

**Files:**
- Create: `src/morty_code/security/os_sandbox.py`
- Modify: `src/morty_code/security/__init__.py`
- Test: `tests/test_os_sandbox.py`

- [ ] Write failing tests for bwrap argv construction, disabled sandbox behavior, missing bwrap fail-closed behavior, and network toggle behavior.
- [ ] Run `UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_os_sandbox.py -q` and confirm the tests fail because the module is missing.
- [ ] Implement `SandboxConfig`, `SandboxUnavailable`, `build_bash_argv`, and `sandbox_metadata`.
- [ ] Export the new symbols from `morty_code.security`.
- [ ] Re-run `UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_os_sandbox.py -q` and confirm green.

### Task 2: Bash Tool Integration

**Files:**
- Modify: `src/morty_code/tools/builtin_tools.py`
- Test: `tests/test_builtin_tools.py`

- [ ] Write failing tests proving `bash` returns sandbox metadata and still executes commands.
- [ ] Run `UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_builtin_tools.py -q` and confirm the new test fails.
- [ ] Replace `asyncio.create_subprocess_shell` with `asyncio.create_subprocess_exec` using `build_bash_argv`.
- [ ] Preserve timeout, stdout/stderr truncation, cwd, and env handling.
- [ ] Re-run focused tests and confirm green.

### Task 3: Verification

**Files:**
- Modify: `docs/claude-security-deep-dive.md`
- Modify: `docs/morty-code-current-state-and-gap-analysis.md`

- [ ] Update docs to move OS sandbox from missing to implemented Linux/bwrap first version.
- [ ] Run `UV_CACHE_DIR=/tmp/uv-cache uv run pytest -q`.
- [ ] Run `UV_CACHE_DIR=/tmp/uv-cache uv run python -m compileall src`.
