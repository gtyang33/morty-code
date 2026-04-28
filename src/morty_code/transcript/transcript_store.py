from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from morty_code.types.messages import Message


class TranscriptStore:
    """append-only transcript 存储。

    第一阶段先实现主链消息落盘。
    第二阶段再补 metadata events、load/rebuild、sidechain。
    """

    def __init__(self, path: Path, session_id: str) -> None:
        self.path = path
        self.session_id = session_id
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @classmethod
    def for_session_dir(cls, session_dir: str | Path) -> "TranscriptStore":
        session_root = Path(session_dir)
        session_root.mkdir(parents=True, exist_ok=True)
        session_id = str(uuid4())
        return cls(session_root / f"{session_id}.jsonl", session_id)

    async def append_messages(
        self,
        messages: list[Message],
        is_sidechain: bool = False,
        starting_parent_uuid: str | None = None,
    ) -> str | None:
        parent_uuid = starting_parent_uuid
        with self.path.open("a", encoding="utf-8") as file:
            for message in messages:
                entry = {
                    "parent_uuid": parent_uuid,
                    "logical_parent_uuid": None,
                    "session_id": self.session_id,
                    "is_sidechain": is_sidechain,
                    "message": asdict(message),
                }
                file.write(json.dumps(entry, ensure_ascii=False) + "\n")
                if message.type in {"user", "assistant", "attachment", "system"}:
                    parent_uuid = message.uuid
        return parent_uuid

    async def append_event(self, event: dict[str, object]) -> None:
        with self.path.open("a", encoding="utf-8") as file:
            entry = {
                "parent_uuid": None,
                "logical_parent_uuid": None,
                "session_id": self.session_id,
                "is_sidechain": False,
                "event": event,
            }
            file.write(json.dumps(entry, ensure_ascii=False) + "\n")
