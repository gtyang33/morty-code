from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal


TaskStatus = Literal["running", "completed", "failed", "interrupted"]


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
    process_id: int | None = None
    heartbeat_at: str | None = None
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())


class SubagentTaskRegistry:
    """进程内 task registry，同时把任务状态写到磁盘。

    这不是完整的 Claude Code task manager；它先提供后台任务最小生命周期：
    launched -> completed/failed，并让父 agent 可通过 output file 查看结果。
    """

    def __init__(self, root: str | Path = ".morty/tasks") -> None:
        """初始化对象状态。"""
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
        """创建新的运行对象或记录。"""
        output_file = str(self.root / f"{task_id}.json")
        task = SubagentTask(
            task_id=task_id,
            agent_id=agent_id,
            agent_type=agent_type,
            description=description,
            prompt=prompt,
            status="running",
            output_file=output_file,
            process_id=os.getpid(),
        )
        self.update(task)
        return task

    def update(self, task: SubagentTask) -> None:
        """处理该方法负责的业务逻辑。"""
        now = datetime.utcnow().isoformat()
        task.updated_at = now
        if task.status == "running":
            task.heartbeat_at = now
        with self._lock:
            self._tasks[task.task_id] = task
            Path(task.output_file).parent.mkdir(parents=True, exist_ok=True)
            Path(task.output_file).write_text(
                json.dumps(asdict(task), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    def get(self, task_id: str) -> SubagentTask | None:
        """获取运行所需数据。"""
        with self._lock:
            cached = self._tasks.get(task_id)
        if cached is not None:
            return cached
        path = self.root / f"{task_id}.json"
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            task = _task_from_payload(payload)
        except (OSError, json.JSONDecodeError, TypeError):
            return None
        with self._lock:
            self._tasks[task.task_id] = task
        return task

    def list(self) -> list[SubagentTask]:
        """列出内存和磁盘上的任务摘要。"""

        tasks: dict[str, SubagentTask] = {}
        for path in sorted(self.root.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                task = _task_from_payload(payload)
            except (OSError, json.JSONDecodeError, TypeError):
                continue
            tasks[task.task_id] = task
        with self._lock:
            tasks.update(self._tasks)
        return sorted(tasks.values(), key=lambda task: task.created_at, reverse=True)

    def format_list(self, limit: int = 20) -> str:
        """格式化输出内容。"""
        tasks = self.list()[:limit]
        if not tasks:
            return "No subagent tasks."
        lines = []
        for task in tasks:
            lines.append(
                f"{task.task_id}  {task.status}  {task.agent_type}  "
                f"{task.description}  output={task.output_file}"
            )
        return "\n".join(lines)

    def interrupt_running(
        self,
        reason: str = "parent process exited",
        process_id: int | None = None,
    ) -> int:
        """把仍在 running 的任务标记为 interrupted。

        进程内后台线程无法在父进程退出后继续执行；退出前显式标记，避免
        重启后 `task_output(block=true)` 对旧任务一直等待到 timeout。
        """

        interrupted = 0
        for task in self.list():
            if task.status != "running":
                continue
            if process_id is not None and task.process_id != process_id:
                continue
            task.status = "interrupted"
            task.error = reason
            self.update(task)
            interrupted += 1
        return interrupted

    def interrupt_orphaned_running(
        self,
        reason: str = "parent process is not running",
    ) -> int:
        """标记 owner 进程已经不存在的 running 任务。

        这是 crash/SIGKILL 后的轻量 reaper。真正的 resume 还没做，所以当前
        策略是诚实地把不可继续执行的任务变成 terminal 状态。
        """

        interrupted = 0
        for task in self.list():
            if task.status != "running":
                continue
            if task.process_id is not None and _pid_is_alive(task.process_id):
                continue
            task.status = "interrupted"
            task.error = reason
            self.update(task)
            interrupted += 1
        return interrupted


_REGISTRY: SubagentTaskRegistry | None = None


def get_subagent_task_registry(root: str | Path = ".morty/tasks") -> SubagentTaskRegistry:
    """获取运行所需数据。"""
    global _REGISTRY
    if _REGISTRY is None or _REGISTRY.root != Path(root):
        _REGISTRY = SubagentTaskRegistry(root)
    return _REGISTRY


def _task_from_payload(payload: dict[str, object]) -> SubagentTask:
    """内部处理该方法负责的业务逻辑。"""
    allowed = set(SubagentTask.__dataclass_fields__)
    return SubagentTask(**{key: value for key, value in payload.items() if key in allowed})


def _pid_is_alive(pid: int) -> bool:
    """内部处理该方法负责的业务逻辑。"""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True
