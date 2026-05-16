from __future__ import annotations

from collections import deque

from morty_code.types.runtime_state import QueuedCommand


class QueueManager:
    """统一用户输入、通知和后台回流消息的调度队列。"""

    def __init__(self) -> None:
        """初始化对象状态。"""
        self._items: deque[QueuedCommand] = deque()

    def enqueue(self, command: QueuedCommand) -> None:
        """处理该方法负责的业务逻辑。"""
        self._items.append(command)

    def extend(self, commands: list[QueuedCommand]) -> None:
        """处理该方法负责的业务逻辑。"""
        self._items.extend(commands)

    def drain(self) -> list[QueuedCommand]:
        """处理该方法负责的业务逻辑。"""
        items = list(self._items)
        self._items.clear()
        return items

    def peek_all(self) -> list[QueuedCommand]:
        """处理该方法负责的业务逻辑。"""
        return list(self._items)

    def __len__(self) -> int:
        """返回对象长度。"""
        return len(self._items)
