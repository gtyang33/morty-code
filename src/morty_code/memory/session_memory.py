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
        if not self.path.exists():
            self.path.write_text("# Session Memory\n", encoding="utf-8")
        with self.path.open("a", encoding="utf-8") as file:
            file.write(f"- {clean}\n")

    def read(self) -> str:
        if not self.path.exists():
            return ""
        return self.path.read_text(encoding="utf-8")
