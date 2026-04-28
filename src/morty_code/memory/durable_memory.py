from __future__ import annotations

from pathlib import Path


class DurableMemoryStore:
    """管理 MEMORY.md 与 topic memory 文件。"""

    def __init__(self, root_dir: str | Path) -> None:
        self.root_dir = Path(root_dir)
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.root_dir / "MEMORY.md"

    def ensure_exists(self) -> None:
        if not self.index_path.exists():
            self.index_path.write_text("# Memory Index\n", encoding="utf-8")

    def append_summary(self, summary: str) -> None:
        self.ensure_exists()
        with self.index_path.open("a", encoding="utf-8") as file:
            file.write(f"- {summary.strip()}\n")
