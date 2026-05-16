from __future__ import annotations

import json
import re
from pathlib import Path

MEMORY_TYPES = {"user", "feedback", "project", "reference"}


class DurableMemoryStore:
    """管理 MEMORY.md 与 topic memory 文件。"""

    def __init__(
        self,
        root_dir: str | Path,
        max_index_lines: int = 200,
        max_index_bytes: int = 24000,
    ) -> None:
        """初始化对象状态。"""
        self.root_dir = Path(root_dir)
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.root_dir / "MEMORY.md"
        self.max_index_lines = max_index_lines
        self.max_index_bytes = max_index_bytes

    def ensure_exists(self) -> None:
        """确保依赖资源处于可用状态。"""
        if not self.index_path.exists():
            self.index_path.write_text("# Memory Index\n", encoding="utf-8")

    def append_summary(self, summary: str, memory_type: str = "project") -> None:
        """追加运行过程产生的数据。"""
        self.ensure_exists()
        clean = " ".join(summary.strip().split())
        if not clean:
            return
        # durable memory 写两份：topic 文件保存完整条目，MEMORY.md 只保存索引。
        # prompt 注入时优先读索引，避免长期记忆无限膨胀。
        topic_path = self._topic_path(clean)
        self._ensure_topic_frontmatter(topic_path, clean, memory_type)
        if self._topic_contains(topic_path, clean):
            return
        with topic_path.open("a", encoding="utf-8") as file:
            file.write(f"- {clean}\n")
        with self.index_path.open("a", encoding="utf-8") as file:
            file.write(f"- [{topic_path.stem}]({topic_path.name}): {clean[:160]}\n")
        self._truncate_index()

    def read_index(self) -> str:
        """读取持久化内容。"""
        self.ensure_exists()
        return self.index_path.read_text(encoding="utf-8")

    def _topic_path(self, summary: str) -> Path:
        # 用摘要前几个词生成稳定 topic 文件名；中英文都保留，便于人工查看。
        """内部处理该方法负责的业务逻辑。"""
        words = re.findall(r"[A-Za-z0-9\u4e00-\u9fff]+", summary.lower())[:8]
        stem = "-".join(words)[:80] or "memory"
        return self.root_dir / f"{stem}.md"

    def _ensure_topic_frontmatter(
        self,
        topic_path: Path,
        summary: str,
        memory_type: str,
    ) -> None:
        """内部确保依赖资源处于可用状态。"""
        if topic_path.exists() and topic_path.read_text(encoding="utf-8", errors="replace").strip():
            return
        clean_type = memory_type if memory_type in MEMORY_TYPES else "project"
        topic_path.write_text(
            "\n".join(
                [
                    "---",
                    f"name: {self._yaml_string(self._frontmatter_name(summary))}",
                    f"description: {self._yaml_string(summary[:180])}",
                    f"type: {clean_type}",
                    "---",
                    "",
                ]
            ),
            encoding="utf-8",
        )

    def _frontmatter_name(self, summary: str) -> str:
        """内部处理该方法负责的业务逻辑。"""
        words = re.findall(r"[A-Za-z0-9\u4e00-\u9fff]+", summary)[:8]
        return " ".join(words)[:80] or "memory"

    def _yaml_string(self, value: str) -> str:
        """内部处理该方法负责的业务逻辑。"""
        return json.dumps(value, ensure_ascii=False)

    def _topic_contains(self, topic_path: Path, summary: str) -> bool:
        """内部处理该方法负责的业务逻辑。"""
        content = topic_path.read_text(encoding="utf-8", errors="replace")
        target = " ".join(summary.lower().split())
        for line in content.splitlines():
            normalized = " ".join(line.removeprefix("-").strip().lower().split())
            if normalized == target:
                return True
        return False

    def _truncate_index(self) -> None:
        # MEMORY.md 是 prompt 热路径的一部分，必须同时限制行数和字节数；
        # 老条目仍在 topic 文件里，只是从索引里淘汰。
        """内部处理该方法负责的业务逻辑。"""
        content = self.index_path.read_text(encoding="utf-8")
        lines = content.splitlines()
        if len(lines) <= self.max_index_lines and len(content.encode("utf-8")) <= self.max_index_bytes:
            return
        header = lines[:1] or ["# Memory Index"]
        body_line_budget = max(0, self.max_index_lines - len(header))
        kept = lines[-body_line_budget:] if body_line_budget > 0 else []
        trimmed = "\n".join([*header, *kept]) + "\n"
        while len(trimmed.encode("utf-8")) > self.max_index_bytes and len(kept) > 1:
            kept = kept[1:]
            trimmed = "\n".join([*header, *kept]) + "\n"
        self.index_path.write_text(trimmed, encoding="utf-8")
