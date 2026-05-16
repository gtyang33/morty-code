from __future__ import annotations

from morty_code.tools.tool_result_budget import PERSISTED_OUTPUT_TAG
from morty_code.transcript.session_restore import SessionRestore
from morty_code.types.messages import Message
from morty_code.types.runtime_state import ContentReplacementState


def test_session_restore_recovers_persisted_output_tool_result() -> None:
    state = ContentReplacementState()
    replacement = f"{PERSISTED_OUTPUT_TAG}\nFull output saved to: /tmp/tool.txt\n</persisted-output>"
    message = Message(
        uuid="tool-result-message",
        timestamp="2026-05-15T00:00:00",
        type="user",
        payload={
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "tool-1",
                    "content": replacement,
                }
            ]
        },
    )

    SessionRestore()._restore_replacements(message, state)

    assert state.seen_ids == {"tool-1"}
    assert state.replacements == {"tool-1": replacement}
