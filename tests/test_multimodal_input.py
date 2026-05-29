from __future__ import annotations

import asyncio
import json
from pathlib import Path

from morty_code.api.model_client import OpenAICompatibleModelClient
from morty_code.input.handle_input import InputDispatcher
from morty_code.input.process_user_input import UserInputProcessor
from morty_code.attachments.attachment_manager import AttachmentManager
from morty_code.compact.auto_compact import AutoCompactDecider
from morty_code.runtime.query_engine import QueryEngine
from morty_code.runtime.query_loop import QueryLoopResult
from morty_code.transcript.transcript_store import TranscriptStore
from morty_code.types.runtime_state import ContentReplacementState, QueuedCommand, ToolUseContext


PNG_BYTES = b"\x89PNG\r\n\x1a\nminimal"


def _write_png(path: Path) -> None:
    path.write_bytes(PNG_BYTES)


def _context(tmp_path: Path) -> ToolUseContext:
    return ToolUseContext(
        tools=[],
        model="test-model",
        permission_mode="default",
        app_state={"cwd": str(tmp_path)},
        read_file_state={},
        content_replacement_state=ContentReplacementState(),
    )


class StubPromptBuilder:
    async def build_for_context(self, context: ToolUseContext) -> tuple[list[str], dict[str, str], dict[str, str]]:
        return ["system"], {}, {}


class RecordingQueryLoop:
    def __init__(self) -> None:
        self.seen_messages = []

    async def run(
        self,
        *,
        messages,
        cache_safe,
        tool_context,
        on_new_messages=None,
    ) -> QueryLoopResult:
        self.seen_messages.append(messages)
        return QueryLoopResult(new_messages=[], metadata_events=[])


def test_dispatcher_converts_direct_image_path_to_image_ref(tmp_path: Path) -> None:
    image = tmp_path / "shot.png"
    _write_png(image)

    command = InputDispatcher(tmp_path).submit_sync("shot.png", "prompt")[0]

    assert command.value == "[Image #1]"
    assert command.pasted_contents is not None
    assert command.pasted_contents[1]["type"] == "image"
    assert command.pasted_contents[1]["media_type"] == "image/png"


def test_dispatcher_converts_markdown_image_path(tmp_path: Path) -> None:
    image = tmp_path / "shot.png"
    _write_png(image)

    command = InputDispatcher(tmp_path).submit_sync("分析 ![截图](shot.png)", "prompt")[0]

    assert command.value == "分析 [Image #1]"
    assert command.pasted_contents is not None
    assert command.pasted_contents[1]["filename"] == "shot.png"


def test_dispatcher_converts_data_url_without_host(tmp_path: Path) -> None:
    command = InputDispatcher(tmp_path).submit_sync(
        "分析 data:image/png;base64,QUJD",
        "prompt",
    )[0]

    assert command.value == "分析 [Image #1]"
    assert command.pasted_contents == {
        1: {
            "id": 1,
            "type": "image",
            "content": "QUJD",
            "media_type": "image/png",
        }
    }


def test_dispatcher_marks_missing_image_reference(tmp_path: Path) -> None:
    command = InputDispatcher(tmp_path).submit_sync("[Image #1] 分析图片", "prompt")[0]

    assert "[Missing image #1: paste or attach the image again]" in command.value
    assert command.pasted_contents is None


def test_inline_image_conversion_tracks_next_paste_id(tmp_path: Path) -> None:
    from morty_code.input.image_input import convert_inline_images

    image = tmp_path / "shot.png"
    _write_png(image)

    converted = convert_inline_images(
        "先看 ![a](shot.png) 再看 data:image/png;base64,QUJD",
        cwd=tmp_path,
        start_id=7,
    )

    assert converted is not None
    assert converted.text == "先看 [Image #7] 再看 [Image #8]"
    assert converted.next_id == 9
    assert sorted(converted.pasted_contents) == [7, 8]


def test_processor_builds_base64_image_source(tmp_path: Path) -> None:
    processor = UserInputProcessor(AttachmentManager())
    command = QueuedCommand(
        value="分析 [Image #1]",
        mode="prompt",
        pasted_contents={
            1: {
                "id": 1,
                "type": "image",
                "content": "QUJD",
                "media_type": "image/png",
            }
        },
    )

    processed = __import__("asyncio").run(
        processor.process(command, _context(tmp_path), [])
    )

    assert processed.messages[0].payload["content"] == [
        {"type": "text", "text": "分析 [Image #1]"},
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": "QUJD",
            },
        },
    ]


def test_openai_client_converts_base64_image_source_to_data_url() -> None:
    client = OpenAICompatibleModelClient(model="test", api_key="key")

    parts = client._to_openai_user_parts(
        [
            {"type": "text", "text": "分析"},
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": "QUJD",
                },
            },
        ]
    )

    assert parts == [
        {"type": "text", "text": "分析"},
        {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64,QUJD"},
        },
    ]


def test_openai_request_body_contains_image_data_url(monkeypatch) -> None:
    captured_body: dict[str, object] = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def readline(self) -> bytes:
            return b"data: [DONE]\n"

    def fake_urlopen(request, timeout):
        captured_body.update(json.loads(request.data.decode("utf-8")))
        return FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = OpenAICompatibleModelClient(
        model="vision-model",
        base_url="https://example.test/v1",
        api_key="key",
    )

    asyncio.run(
        client.respond(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "分析 [Image #1]"},
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": "QUJD",
                            },
                        },
                    ],
                }
            ],
            [],
            {},
            {},
        )
    )

    user_message = captured_body["messages"][1]
    assert user_message["content"][1] == {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,QUJD"},
    }


def test_query_engine_passes_pasted_image_to_query_loop(tmp_path: Path) -> None:
    query_loop = RecordingQueryLoop()
    engine = QueryEngine(
        prompt_builder=StubPromptBuilder(),
        input_dispatcher=InputDispatcher(tmp_path),
        input_processor=UserInputProcessor(AttachmentManager()),
        query_loop=query_loop,
        transcript_store=TranscriptStore(tmp_path / "session.jsonl", "session"),
        auto_compact_decider=AutoCompactDecider(token_threshold=999999),
    )

    engine.submit_message_sync(
        "[Image #1] 分析图片",
        _context(tmp_path),
        pasted_contents={
            1: {
                "id": 1,
                "type": "image",
                "content": "QUJD",
                "media_type": "image/png",
            }
        },
    )

    content = query_loop.seen_messages[0][0].payload["content"]
    assert content[0] == {"type": "text", "text": "[Image #1] 分析图片"}
    assert content[1]["type"] == "image"
    assert content[1]["source"]["data"] == "QUJD"

    events = [
        json.loads(line)
        for line in (tmp_path / "session.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    image_event = next(
        event["event"]
        for event in events
        if event.get("event", {}).get("type") == "input-images-attached"
    )
    assert image_event["images"] == [
        {"index": 2, "media_type": "image/png", "base64_chars": 4}
    ]
