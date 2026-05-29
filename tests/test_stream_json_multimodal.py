from __future__ import annotations

from morty_code.harness.stream_json import parse_stream_json_user_event


def test_stream_json_user_event_converts_image_content_blocks() -> None:
    parsed = parse_stream_json_user_event(
        {
            "type": "user",
            "message": {
                "content": [
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
            },
        }
    )

    assert parsed.text == "分析 [Image #1]"
    assert parsed.pasted_contents == {
        1: {
            "id": 1,
            "type": "image",
            "content": "QUJD",
            "media_type": "image/png",
        }
    }
