from __future__ import annotations

from pathlib import Path

from morty_code.types.messages import Attachment


class RelevantMemoryFinder:
    """第一阶段使用轻量规则而不是复杂检索。"""

    def __init__(self, root_dir: str | Path) -> None:
        self.root_dir = Path(root_dir)

    def find(self, input_text: str) -> list[Attachment]:
        if not self.root_dir.exists():
            return []
        matches: list[Attachment] = []
        lowered = input_text.lower()
        for path in sorted(self.root_dir.glob("*.md"))[:5]:
            name = path.stem.lower()
            if name and name in lowered:
                matches.append(
                    Attachment(
                        type="relevant_memories",
                        payload={
                            "path": str(path),
                            "content": path.read_text(encoding="utf-8"),
                        },
                        is_meta=True,
                    )
                )
        return matches
