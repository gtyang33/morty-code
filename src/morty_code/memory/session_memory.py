from __future__ import annotations

from pathlib import Path


class SessionMemoryStore:
    """当前 session 的工作笔记层。"""

    def __init__(
        self,
        path: str | Path,
        max_chars: int = 8000,
        max_notes: int = 80,
    ) -> None:
        """初始化对象状态。"""
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_chars = max_chars
        self.max_notes = max_notes

    def append_note(self, note: str) -> None:
        """追加运行过程产生的数据。"""
        clean = note.strip()
        if not clean:
            return
        # session memory 是当前会话的轻量工作笔记，适合保存“本轮任务里刚发现”
        # 的事实；它和 durable memory 分开，避免临时噪声长期污染全局记忆。
        if not self.path.exists():
            self.path.write_text("# Session Memory\n", encoding="utf-8")
        with self.path.open("a", encoding="utf-8") as file:
            file.write(f"- {clean}\n")
        self._prune()

    def read(self) -> str:
        """读取持久化内容。"""
        if not self.path.exists():
            return ""
        return self.path.read_text(encoding="utf-8")

    def _prune(self) -> None:
        """内部处理该方法负责的业务逻辑。"""
        content = self.path.read_text(encoding="utf-8")
        lines = content.splitlines()
        header = lines[:1] if lines and lines[0].startswith("#") else ["# Session Memory"]
        notes = [line for line in lines[1:] if line.strip()]
        kept = notes[-self.max_notes :] if self.max_notes > 0 else []
        while kept:
            candidate = "\n".join([*header, *kept]) + "\n"
            if len(candidate) <= self.max_chars:
                self.path.write_text(candidate, encoding="utf-8")
                return
            kept = kept[1:]
        self.path.write_text("\n".join(header) + "\n", encoding="utf-8")
