from __future__ import annotations

import json
import sys
from typing import TextIO
from uuid import uuid4

from morty_code.harness.sdk_events import message_to_sdk_event, result_event
from morty_code.security.permission_settings import SUPPORTED_PERMISSION_MODES
from morty_code.types.runtime_state import ToolUseContext


def run_stream_json_harness(
    engine,
    tool_context: ToolUseContext,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
) -> None:
    """运行本地 NDJSON harness。

    输入协议：
    - `{"type":"user","message":{"content":"..."}}`
    - `{"type":"control_request","request_id":"...","request":{"subtype":"initialize"}}`
    - `set_model` / `set_permission_mode` / `interrupt`

    输出协议：
    - assistant/user/system/result events
    - control_response events
    """

    input_stream = stdin or sys.stdin
    output_stream = stdout or sys.stdout
    tool_context.app_state["permission_request_handler"] = (
        lambda request: _request_tool_permission(request, tool_context, input_stream, output_stream)
    )
    _write_event(output_stream, _initialized_event(tool_context))
    for raw_line in input_stream:
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            _write_event(output_stream, result_event(session_id=_session_id(tool_context), success=False, error=str(exc)))
            continue
        if not isinstance(event, dict):
            _write_event(output_stream, result_event(session_id=_session_id(tool_context), success=False, error="event must be an object"))
            continue
        if event.get("type") == "control_request":
            _handle_control_request(event, tool_context, output_stream)
            continue
        if event.get("type") != "user":
            _write_event(output_stream, result_event(session_id=_session_id(tool_context), success=False, error=f"unsupported event type: {event.get('type')}"))
            continue
        text = _extract_user_text(event)
        if not text:
            _write_event(output_stream, result_event(session_id=_session_id(tool_context), success=False, error="user message content is empty"))
            continue
        try:
            messages = engine.submit_message_sync(text, tool_context)
            for message in messages:
                _write_event(output_stream, message_to_sdk_event(message, _session_id(tool_context)))
            _write_event(output_stream, result_event(session_id=_session_id(tool_context)))
        except Exception as exc:  # noqa: BLE001 - harness 不能让异常破坏 JSONL 协议。
            _write_event(output_stream, result_event(session_id=_session_id(tool_context), success=False, error=str(exc)))


def _handle_control_request(
    event: dict[str, object],
    tool_context: ToolUseContext,
    stdout: TextIO,
) -> None:
    """内部处理该方法负责的业务逻辑。"""
    request_id = str(event.get("request_id") or uuid4())
    request = event.get("request")
    if not isinstance(request, dict):
        _write_control_error(stdout, request_id, "control_request.request must be an object")
        return
    subtype = str(request.get("subtype") or "")
    if subtype == "initialize":
        _write_control_success(
            stdout,
            request_id,
            {
                "commands": [],
                "agents": [],
                "output_style": "normal",
                "available_output_styles": ["normal"],
                "models": [{"id": tool_context.model, "name": tool_context.model}],
                "account": {},
                "pid": None,
            },
        )
        return
    if subtype == "set_model":
        model = request.get("model")
        if model:
            tool_context.model = str(model)
        _write_control_success(stdout, request_id)
        return
    if subtype == "set_permission_mode":
        mode = str(request.get("mode") or "")
        if mode not in SUPPORTED_PERMISSION_MODES:
            _write_control_error(stdout, request_id, f"unsupported permission mode: {mode}")
            return
        tool_context.permission_mode = mode
        tool_context.app_state["permission_mode"] = mode
        tool_context.app_state["plan_mode"] = mode == "plan"
        _write_control_success(stdout, request_id)
        return
    if subtype == "interrupt":
        # 当前 query 是同步执行的，收到 interrupt 时没有独立可取消任务；返回成功保持协议不阻塞。
        _write_control_success(stdout, request_id)
        return
    if subtype == "get_status":
        _write_control_success(
            stdout,
            request_id,
            {
                "model": tool_context.model,
                "permission_mode": tool_context.permission_mode,
                "tools": tool_context.tools,
                "session_id": _session_id(tool_context),
            },
        )
        return
    _write_control_error(stdout, request_id, f"unsupported control_request subtype: {subtype}")


def _request_tool_permission(
    request: dict[str, object],
    tool_context: ToolUseContext,
    stdin: TextIO,
    stdout: TextIO,
) -> dict[str, object]:
    """内部处理该方法负责的业务逻辑。"""
    request_id = str(uuid4())
    _write_event(
        stdout,
        {
            "type": "control_request",
            "request_id": request_id,
            "request": {
                "subtype": "can_use_tool",
                "tool_name": request.get("tool_name"),
                "input": request.get("input") if isinstance(request.get("input"), dict) else {},
                "tool_use_id": request.get("tool_use_id") or "",
                "decision_reason": request.get("decision_reason") or "",
                "description": request.get("message") or "",
                "permission_suggestions": [],
                "agent_id": _session_id(tool_context),
            },
        },
    )
    for raw_line in stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "control_response":
            _write_event(
                stdout,
                result_event(
                    session_id=_session_id(tool_context),
                    success=False,
                    error="only control_response is accepted while waiting for tool permission",
                ),
            )
            continue
        response = event.get("response")
        if not isinstance(response, dict):
            continue
        if str(response.get("request_id") or "") != request_id:
            continue
        if response.get("subtype") == "error":
            return {"behavior": "deny", "message": response.get("error") or "permission response error"}
        payload = response.get("response")
        if not isinstance(payload, dict):
            return {"behavior": "deny", "message": "permission response missing decision payload"}
        behavior = str(payload.get("behavior") or "deny")
        if behavior == "allow":
            result = {"behavior": "allow"}
            if isinstance(payload.get("updatedInput"), dict):
                result["updatedInput"] = payload["updatedInput"]
            return result
        return {
            "behavior": "deny",
            "message": str(payload.get("message") or "permission denied by harness"),
        }
    return {"behavior": "deny", "message": "permission response stream closed"}


def _extract_user_text(event: dict[str, object]) -> str:
    """内部提取后续流程需要的信息。"""
    message = event.get("message")
    if isinstance(message, dict):
        content = message.get("content")
    else:
        content = event.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(part for part in parts if part).strip()
    return ""


def _initialized_event(tool_context: ToolUseContext) -> dict[str, object]:
    """内部处理该方法负责的业务逻辑。"""
    return {
        "type": "system",
        "subtype": "harness_initialized",
        "session_id": _session_id(tool_context),
        "model": tool_context.model,
        "permission_mode": tool_context.permission_mode,
        "tools": tool_context.tools,
    }


def _write_control_success(
    stdout: TextIO,
    request_id: str,
    response: dict[str, object] | None = None,
) -> None:
    """内部写入持久化内容。"""
    payload: dict[str, object] = {
        "type": "control_response",
        "response": {
            "subtype": "success",
            "request_id": request_id,
        },
    }
    if response is not None:
        payload["response"]["response"] = response
    _write_event(stdout, payload)


def _write_control_error(stdout: TextIO, request_id: str, error: str) -> None:
    """内部写入持久化内容。"""
    _write_event(
        stdout,
        {
            "type": "control_response",
            "response": {
                "subtype": "error",
                "request_id": request_id,
                "error": error,
            },
        },
    )


def _write_event(stdout: TextIO, event: dict[str, object]) -> None:
    """内部写入持久化内容。"""
    stdout.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
    stdout.flush()


def _session_id(tool_context: ToolUseContext) -> str:
    """内部处理该方法负责的业务逻辑。"""
    return str(tool_context.app_state.get("session_id") or "default")
