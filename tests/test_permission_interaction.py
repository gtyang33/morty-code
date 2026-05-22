from __future__ import annotations

from morty_code.security.permission_ui import build_permission_request
from morty_code.ui import format_interaction_request, resolve_choice


def test_permission_interaction_shows_tool_details_and_input() -> None:
    request = build_permission_request(
        {
            "tool_name": "bash",
            "decision_reason": "rule",
            "message": "Bash requires approval.",
            "input": {"command": "git status"},
        }
    )
    rendered = format_interaction_request(request)

    assert "Tool permission required" in rendered
    assert "Tool: bash" in rendered
    assert "Bash requires approval." in rendered
    assert '"command": "git status"' in rendered
    assert resolve_choice("1", request.choices) == "allow"
    assert resolve_choice("no", request.choices) == "deny"
