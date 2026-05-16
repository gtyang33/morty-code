from __future__ import annotations

from pathlib import Path

from morty_code.types.messages import Attachment


class RelevantMemoryFinder:
    """带预算控制的轻量 relevant memory 检索。"""

    def __init__(
        self,
        root_dir: str | Path,
        max_files: int = 5,
        max_file_chars: int = 6000,
        max_total_chars: int = 18000,
    ) -> None:
        """初始化对象状态。"""
        self.root_dir = Path(root_dir)
        self.max_files = max_files
        self.max_file_chars = max_file_chars
        self.max_total_chars = max_total_chars

    def find(self, input_text: str) -> list[Attachment]:
        """查找匹配的注册项或数据。"""
        if not self.root_dir.exists():
            return []
        tokens = self._tokens(input_text.lower())
        if not tokens:
            return []
        scored: list[tuple[int, Path, str]] = []
        for path in sorted(self.root_dir.glob("*.md")):
            if path.name == "MEMORY.md":
                # MEMORY.md 是轻量索引，不作为 topic 正文重复注入。
                continue
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            haystack = f"{path.stem}\n{content}".lower()
            score = self._score(tokens, haystack)
            if score > 0:
                scored.append((score, path, content))
        scored.sort(key=lambda item: (-item[0], str(item[1])))
        matches: list[Attachment] = []
        used_chars = 0
        for _, path, content in scored:
            remaining = self.max_total_chars - used_chars
            if remaining <= 0 or len(matches) >= self.max_files:
                break
            visible = content[: min(self.max_file_chars, remaining)]
            used_chars += len(visible)
            matches.append(
                Attachment(
                    type="relevant_memories",
                    payload={
                        "path": str(path),
                        "content": visible,
                        "truncated": len(content) > len(visible),
                    },
                    is_meta=True,
                )
            )
        return matches

    def _tokens(self, lowered_input: str) -> set[str]:
        """内部处理该方法负责的业务逻辑。"""
        return {
            token
            for token in lowered_input.replace("/", " ").replace("_", " ").split()
            if len(token) >= 3
        }

    def _score(self, tokens: set[str], lowered_memory: str) -> int:
        """内部处理该方法负责的业务逻辑。"""
        return sum(1 for token in tokens if token in lowered_memory)
