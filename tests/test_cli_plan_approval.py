from __future__ import annotations

from morty_code.plan.approval_ui import build_plan_approval_request, pending_plan_approval
from morty_code.types.runtime_state import ContentReplacementState, ToolUseContext
from morty_code.ui import TerminalInteraction, format_interaction_request, resolve_choice


def make_context() -> ToolUseContext:
    return ToolUseContext(
        tools=[],
        model="test-model",
        permission_mode="plan",
        app_state={
            "pending_plan_approval": {
                "status": "awaiting_user_approval",
                "plan": "1. 修改输入层\n2. 增加测试\n3. 跑全量验证",
            }
        },
        read_file_state={},
        content_replacement_state=ContentReplacementState(),
    )


def test_plan_approval_action_resolver_supports_keyboard_fallback_inputs() -> None:
    request = build_plan_approval_request(make_context())

    assert resolve_choice("1", request.choices) == "approve"
    assert resolve_choice("ok", request.choices) == "approve"
    assert resolve_choice("2", request.choices) == "changes"
    assert resolve_choice("修改", request.choices) == "changes"
    assert resolve_choice("3", request.choices) == "reject"
    assert resolve_choice("", request.choices) is None


def test_plan_approval_request_includes_full_pending_plan() -> None:
    request = build_plan_approval_request(make_context())
    rendered = format_interaction_request(request)

    assert "Implementation plan ready" in rendered
    assert "Review the full plan below" in rendered
    assert "1. 修改输入层" in rendered
    assert "3. 跑全量验证" in rendered
    assert "Approve and implement" in rendered


def test_pending_plan_approval_returns_only_dict_state() -> None:
    context = make_context()
    assert pending_plan_approval(context)["status"] == "awaiting_user_approval"
    context.app_state["pending_plan_approval"] = "bad"
    assert pending_plan_approval(context) is None


def test_terminal_interaction_enter_binding_is_eager() -> None:
    request = build_plan_approval_request(make_context())
    interaction = TerminalInteraction()
    app = interaction._build_application(request)

    confirm_bindings = [
        binding
        for binding in app.key_bindings.bindings
        if any(key.value in {"enter", "c-m"} for key in binding.keys)
    ]

    assert confirm_bindings
    assert any(binding.eager() for binding in confirm_bindings)


def test_terminal_interaction_detail_area_accepts_mouse_focus_for_scroll() -> None:
    detail = TerminalInteraction()._build_detail_area("line 1\nline 2")

    assert detail.control.focusable()
    assert detail.control.focus_on_click()
    assert detail.window.right_margins
