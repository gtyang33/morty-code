from __future__ import annotations

import json
from io import StringIO

from morty_code.harness import stream_json
from morty_code.types.messages import Message
from morty_code.types.runtime_state import ContentReplacementState, ToolUseContext


class FakeEngine:
    def submit_message_sync(self, text: str, tool_context: ToolUseContext) -> list[Message]:
        return [
            Message(
                uuid="assistant-1",
                timestamp="2026-05-03T00:00:00",
                type="assistant",
                payload={"content": [{"type": "text", "text": f"Echo: {text}"}]},
            )
        ]


def make_context() -> ToolUseContext:
    return ToolUseContext(
        tools=["bash"],
        model="test-model",
        permission_mode="default",
        app_state={"session_id": "session-1"},
        read_file_state={},
        content_replacement_state=ContentReplacementState(),
    )


def parse_jsonl(output: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in output.splitlines() if line.strip()]


def test_stream_json_harness_handles_initialize_user_and_result() -> None:
    stdin = StringIO(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "control_request",
                        "request_id": "init-1",
                        "request": {"subtype": "initialize"},
                    }
                ),
                json.dumps({"type": "user", "message": {"content": "hello"}}),
            ]
        )
        + "\n"
    )
    stdout = StringIO()

    stream_json.run_stream_json_harness(FakeEngine(), make_context(), stdin=stdin, stdout=stdout)

    events = parse_jsonl(stdout.getvalue())
    assert events[0]["type"] == "system"
    assert events[0]["subtype"] == "harness_initialized"
    assert events[1]["type"] == "control_response"
    assert events[1]["response"]["subtype"] == "success"
    assert events[2]["type"] == "assistant"
    assert events[2]["message"]["content"][0]["text"] == "Echo: hello"
    assert events[3]["type"] == "result"
    assert events[3]["subtype"] == "success"


def test_stream_json_harness_set_permission_mode_updates_status() -> None:
    stdin = StringIO(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "control_request",
                        "request_id": "mode-1",
                        "request": {"subtype": "set_permission_mode", "mode": "plan"},
                    }
                ),
                json.dumps(
                    {
                        "type": "control_request",
                        "request_id": "status-1",
                        "request": {"subtype": "get_status"},
                    }
                ),
            ]
        )
        + "\n"
    )
    stdout = StringIO()

    stream_json.run_stream_json_harness(FakeEngine(), make_context(), stdin=stdin, stdout=stdout)

    events = parse_jsonl(stdout.getvalue())
    status_response = events[2]["response"]["response"]
    assert status_response["permission_mode"] == "plan"


def test_harness_permission_request_round_trip_allows_updated_input() -> None:
    stream_json.uuid4 = lambda: "perm-1"
    stdin = StringIO(
        json.dumps(
            {
                "type": "control_response",
                "response": {
                    "subtype": "success",
                    "request_id": "perm-1",
                    "response": {
                        "behavior": "allow",
                        "updatedInput": {"command": "printf changed"},
                    },
                },
            }
        )
        + "\n"
    )
    stdout = StringIO()

    response = stream_json._request_tool_permission(
        {
            "tool_name": "bash",
            "input": {"command": "printf old"},
            "tool_use_id": "tool-1",
            "decision_reason": "rule",
            "message": "needs approval",
        },
        make_context(),
        stdin,
        stdout,
    )

    request_event = parse_jsonl(stdout.getvalue())[0]
    assert request_event["type"] == "control_request"
    assert request_event["request_id"] == "perm-1"
    assert request_event["request"]["subtype"] == "can_use_tool"
    assert response == {"behavior": "allow", "updatedInput": {"command": "printf changed"}}
