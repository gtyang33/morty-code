from __future__ import annotations

from pathlib import Path


class SessionMemoryStore:
    """当前 session 的工作笔记层。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append_note(self, note: str) -> None:
        clean = note.strip()
        if not clean:
            return
        # session memory 是当前会话的轻量工作笔记，适合保存“本轮任务里刚发现”
        # 的事实；它和 durable memory 分开，避免临时噪声长期污染全局记忆。
        if not self.path.exists():
            self.path.write_text("# Session Memory\n", encoding="utf-8")
        with self.path.open("a", encoding="utf-8") as file:
            file.write(f"- {clean}\n")

    def read(self) -> str:
        if not self.path.exists():
            return ""
        return self.path.read_text(encoding="utf-8")
