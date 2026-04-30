from __future__ import annotations

import re
from pathlib import Path


class PlanStore:
    """按 session 管理 plan 文件。

    Claude Code 使用独立 plan file 承载审批内容。这里保持同样方向，
    但用 session_id 生成稳定文件名，避免再引入随机 slug 缓存。
    """

    def __init__(self, plans_dir: str | Path, session_id: str) -> None:
        self.plans_dir = Path(plans_dir)
        self.session_id = session_id

    @classmethod
    def from_app_state(cls, app_state: dict[str, object]) -> "PlanStore":
        session_id = str(app_state.get("session_id") or "default")
        plans_dir = Path(str(app_state.get("plans_dir") or ".morty/plans"))
        return cls(plans_dir=plans_dir, session_id=session_id)

    @property
    def path(self) -> Path:
        slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", self.session_id).strip("-") or "default"
        return self.plans_dir / f"{slug}.md"

    def ensure(self) -> Path:
        self.plans_dir.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text("", encoding="utf-8")
        return self.path

    def read(self) -> str:
        try:
            return self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    def write(self, content: str) -> Path:
        self.plans_dir.mkdir(parents=True, exist_ok=True)
        self.path.write_text(content.rstrip() + "\n", encoding="utf-8")
        return self.path

    def has_plan(self) -> bool:
        return bool(self.read().strip())
