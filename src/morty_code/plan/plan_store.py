from __future__ import annotations

import re
from pathlib import Path


class PlanStore:
    """按 session 管理 plan 文件。

    Claude Code 使用独立 plan file 承载审批内容。这里保持同样方向，
    但用 session_id 生成稳定文件名，避免再引入随机 slug 缓存。
    """

    def __init__(self, plans_dir: str | Path, session_id: str) -> None:
        """初始化对象状态。"""
        self.plans_dir = Path(plans_dir)
        self.session_id = session_id

    @classmethod
    def from_app_state(cls, app_state: dict[str, object]) -> "PlanStore":
        """从外部状态构建对象。"""
        session_id = str(app_state.get("session_id") or "default")
        plans_dir = Path(str(app_state.get("plans_dir") or ".morty/plans"))
        return cls(plans_dir=plans_dir, session_id=session_id)

    @property
    def path(self) -> Path:
        # plan 文件按 session 隔离，避免同一 workspace 多个会话互相覆盖计划。
        # session_id 可能来自用户指定文件名，因此先做 slug 清洗。
        """处理该方法负责的业务逻辑。"""
        slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", self.session_id).strip("-") or "default"
        return self.plans_dir / f"{slug}.md"

    def ensure(self) -> Path:
        # 进入 plan mode 时先创建空文件，方便用户或模型随后通过工具增量写入。
        """确保依赖资源处于可用状态。"""
        self.plans_dir.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text("", encoding="utf-8")
        return self.path

    def read(self) -> str:
        """读取持久化内容。"""
        try:
            return self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    def write(self, content: str) -> Path:
        # 统一补一个末尾换行，减少后续追加/展示时出现格式抖动。
        """写入持久化内容。"""
        self.plans_dir.mkdir(parents=True, exist_ok=True)
        self.path.write_text(content.rstrip() + "\n", encoding="utf-8")
        return self.path

    def has_plan(self) -> bool:
        """判断当前对象是否包含目标内容。"""
        return bool(self.read().strip())
