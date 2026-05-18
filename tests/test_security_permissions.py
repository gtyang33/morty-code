from __future__ import annotations

import json

import pytest

from morty_code.security import (
    SecurityViolation,
    assert_safe_bash_command,
    assert_safe_write_path,
    evaluate_tool_permission,
    load_permission_settings,
    parse_tool_rule,
    shell_pattern_matches,
)
from morty_code.types.runtime_state import ContentReplacementState, ToolUseContext


def make_context(
    *,
    mode: str = "default",
    app_state: dict[str, object] | None = None,
) -> ToolUseContext:
    return ToolUseContext(
        tools=[],
        model="test-model",
        permission_mode=mode,
        app_state=app_state or {},
        read_file_state={},
        content_replacement_state=ContentReplacementState(),
    )


def test_shell_rule_parser_handles_tool_content_and_escaped_parentheses() -> None:
    rule = parse_tool_rule(r"Bash(python -c \"print\(1\)\")")

    assert rule.tool_name == "Bash"
    assert rule.content == r"python -c \"print(1)\""


def test_shell_patterns_match_exact_prefix_wildcard_and_escaped_star() -> None:
    assert shell_pattern_matches("git status", "git status")
    assert not shell_pattern_matches("git status", "git status --short")
    assert shell_pattern_matches("uv run pytest:*", "uv run pytest tests")
    assert shell_pattern_matches("git *", "git")
    assert shell_pattern_matches("git *", "git status")
    assert shell_pattern_matches(r"echo \*", "echo *")
    assert not shell_pattern_matches(r"echo \*", "echo value")


def test_permission_settings_merge_shared_local_and_env(tmp_path) -> None:
    settings_dir = tmp_path / ".morty"
    settings_dir.mkdir()
    (settings_dir / "permissions.json").write_text(
        json.dumps(
            {
                "permissions": {
                    "defaultMode": "acceptEdits",
                    "allow": ["read_file"],
                    "ask": ["Bash(git push:*)"],
                }
            }
        ),
        encoding="utf-8",
    )
    (settings_dir / "permissions.local.json").write_text(
        json.dumps({"permissions": {"deny": ["spawn_agent"]}}),
        encoding="utf-8",
    )

    settings = load_permission_settings(
        tmp_path,
        env_allow=["list_files"],
        env_default_mode="plan",
    )

    assert settings.default_mode == "plan"
    assert settings.allow == ["read_file", "list_files"]
    assert settings.ask == ["Bash(git push:*)"]
    assert settings.deny == ["spawn_agent"]
    assert len(settings.sources) == 4


def test_permission_policy_prioritizes_content_deny_over_tool_allow() -> None:
    context = make_context(
        app_state={
            "always_allowed_tools": ["bash"],
            "denied_tools": ["Bash(git reset:*)"],
        }
    )

    denied = evaluate_tool_permission("bash", {"command": "git reset --hard"}, context)
    allowed = evaluate_tool_permission("bash", {"command": "printf ok"}, context)

    assert denied.behavior == "deny"
    assert "Bash(git reset:*)" in denied.message
    assert allowed.behavior == "allow"


def test_permission_policy_keeps_content_ask_in_bypass_mode() -> None:
    context = make_context(
        mode="bypassPermissions",
        app_state={"always_ask_tools": ["Bash(git push:*)"]},
    )

    ask = evaluate_tool_permission("bash", {"command": "git push origin main"}, context)
    bypassed = evaluate_tool_permission("bash", {"command": "git status"}, context)

    assert ask.behavior == "ask"
    assert bypassed.behavior == "allow"


def test_plan_mode_blocks_mutating_tools() -> None:
    context = make_context(mode="plan")

    decision = evaluate_tool_permission("write_file", {"path": "x"}, context)

    assert decision.behavior == "deny"
    assert "plan mode" in decision.message


def test_plan_mode_allows_writing_only_the_plan_file(tmp_path) -> None:
    plan_path = tmp_path / ".morty" / "plans" / "session.md"
    context = make_context(
        mode="plan",
        app_state={
            "cwd": str(tmp_path),
            "plan_mode": True,
            "plan_file_path": str(plan_path),
        },
    )

    allowed = evaluate_tool_permission(
        "write_file",
        {"path": ".morty/plans/session.md"},
        context,
    )
    denied = evaluate_tool_permission(
        "write_file",
        {"path": "src/app.py"},
        context,
    )

    assert allowed.behavior == "allow"
    assert denied.behavior == "deny"
    assert "only the plan file" in denied.message


def test_tool_security_blocks_sensitive_writes_and_dangerous_bash(tmp_path) -> None:
    with pytest.raises(SecurityViolation):
        assert_safe_write_path(tmp_path, tmp_path / ".env")

    with pytest.raises(SecurityViolation):
        assert_safe_bash_command("rm -rf .", root=tmp_path)
