from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from morty_code.types.messages import Message
from morty_code.types.runtime_state import LoadedTranscript


class TranscriptStore:
    """append-only transcript 存储，主链和 sidechain parent 分开维护。"""

    def __init__(self, path: Path, session_id: str) -> None:
        self.path = path
        self.session_id = session_id
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._last_parent_uuid: str | None = None
        self._last_sidechain_parent_uuid: str | None = None

    @classmethod
    def for_session_dir(cls, session_dir: str | Path) -> "TranscriptStore":
        # 新会话使用随机 uuid 做文件名，避免同一 workspace 同时启动多个
        # morty-code 实例时互相覆盖 transcript。
        session_root = Path(session_dir)
        session_root.mkdir(parents=True, exist_ok=True)
        session_id = str(uuid4())
        return cls(session_root / f"{session_id}.jsonl", session_id)

    @classmethod
    def latest_in_session_dir(cls, session_dir: str | Path) -> "TranscriptStore | None":
        """恢复最近一次主会话 transcript，用于 CLI `-c/--continue`。

        这里只扫描当前 workspace 的 `.morty/sessions/*.jsonl`，不跨项目查找。
        这和 Claude Code 的行为一致：`-c` 的语义是“继续这个项目最近一次会话”，
        不是全局最近会话。
        """

        session_root = Path(session_dir)
        if not session_root.exists():
            return None
        # 过滤空文件：进程启动后可能已经创建 transcript 路径，但还没成功写入
        # 任何消息。恢复这种文件会让用户误以为上下文存在，实际是空会话。
        candidates = [
            path for path in session_root.glob("*.jsonl")
            if path.is_file() and path.stat().st_size > 0
        ]
        if not candidates:
            return None
        # mtime 表示最近写入时间；文件名作为稳定 tie-breaker，避免同一时间戳
        # 下不同文件系统返回顺序不一致。
        latest = max(candidates, key=lambda path: (path.stat().st_mtime_ns, path.name))
        return cls(latest, latest.stem)

    async def append_messages(
        self,
        messages: list[Message],
        is_sidechain: bool = False,
        starting_parent_uuid: str | None = None,
    ) -> str | None:
        # parent_uuid 是 transcript 恢复和 UI 串联的基础。主链和 sidechain
        # 分别维护 parent，避免子代理写入打乱父会话的线性上下文。
        if starting_parent_uuid is not None:
            parent_uuid = starting_parent_uuid
        elif is_sidechain:
            parent_uuid = self._last_sidechain_parent_uuid
        else:
            parent_uuid = self._last_parent_uuid
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
        if is_sidechain:
            self._last_sidechain_parent_uuid = parent_uuid
        else:
            self._last_parent_uuid = parent_uuid
        return parent_uuid

    async def append_event(self, event: dict[str, object]) -> None:
        # event 不参与模型上下文，只用于恢复、诊断和后续统计；仍放进同一个
        # append-only 文件，保证一条 session 的行为轨迹可以单文件审计。
        with self.path.open("a", encoding="utf-8") as file:
            entry = {
                "parent_uuid": None,
                "logical_parent_uuid": None,
                "session_id": self.session_id,
                "is_sidechain": False,
                "event": event,
            }
            file.write(json.dumps(entry, ensure_ascii=False) + "\n")

    async def load_session(self, include_sidechains: bool = False) -> LoadedTranscript:
        # 默认只加载主链。sidechain 是子代理/分叉执行细节，直接混进主链会让
        # 模型看到重复或不该继承的探索过程。
        messages: list[Message] = []
        events: list[dict[str, object]] = []
        last_parent_uuid: str | None = None
        if not self.path.exists():
            return LoadedTranscript(messages=[], metadata_events=[], last_parent_uuid=None)
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            entry = json.loads(line)
            if "message" in entry:
                if entry.get("is_sidechain") and not include_sidechains:
                    continue
                message = Message(**entry["message"])
                messages.append(message)
                last_parent_uuid = message.uuid
            elif "event" in entry:
                events.append(entry["event"])
        return LoadedTranscript(
            messages=messages,
            metadata_events=events,
            last_parent_uuid=last_parent_uuid,
        )
