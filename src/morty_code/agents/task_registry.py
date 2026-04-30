from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal


TaskStatus = Literal["running", "completed", "failed"]


@dataclass
class SubagentTask:
    """后台子代理任务的持久化摘要。"""

    task_id: str
    agent_id: str
    agent_type: str
    description: str
    prompt: str
    status: TaskStatus
    output_file: str
    transcript_path: str | None = None
    output: str = ""
    error: str = ""
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())


class SubagentTaskRegistry:
    """进程内 task registry，同时把任务状态写到磁盘。

    这不是完整的 Claude Code task manager；它先提供后台任务最小生命周期：
    launched -> completed/failed，并让父 agent 可通过 output file 查看结果。
    """

    def __init__(self, root: str | Path = ".morty/tasks") -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._tasks: dict[str, SubagentTask] = {}
        self._lock = threading.Lock()

    def create(
        self,
        *,
        task_id: str,
        agent_id: str,
        agent_type: str,
        description: str,
        prompt: str,
    ) -> SubagentTask:
        output_file = str(self.root / f"{task_id}.json")
        task = SubagentTask(
            task_id=task_id,
            agent_id=agent_id,
            agent_type=agent_type,
            description=description,
            prompt=prompt,
            status="running",
            output_file=output_file,
        )
        self.update(task)
        return task

    def update(self, task: SubagentTask) -> None:
        task.updated_at = datetime.utcnow().isoformat()
        with self._lock:
            self._tasks[task.task_id] = task
            Path(task.output_file).parent.mkdir(parents=True, exist_ok=True)
            Path(task.output_file).write_text(
                json.dumps(asdict(task), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    def get(self, task_id: str) -> SubagentTask | None:
        with self._lock:
            return self._tasks.get(task_id)


_REGISTRY: SubagentTaskRegistry | None = None


def get_subagent_task_registry(root: str | Path = ".morty/tasks") -> SubagentTaskRegistry:
    global _REGISTRY
    if _REGISTRY is None or _REGISTRY.root != Path(root):
        _REGISTRY = SubagentTaskRegistry(root)
    return _REGISTRY
