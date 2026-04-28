from __future__ import annotations

from collections import deque

from morty_code.types.runtime_state import QueuedCommand


class QueueManager:
    """统一用户输入、通知和后台回流消息的调度队列。"""

    def __init__(self) -> None:
        self._items: deque[QueuedCommand] = deque()

    def enqueue(self, command: QueuedCommand) -> None:
        self._items.append(command)

    def extend(self, commands: list[QueuedCommand]) -> None:
        self._items.extend(commands)

    def drain(self) -> list[QueuedCommand]:
        items = list(self._items)
        self._items.clear()
        return items

    def peek_all(self) -> list[QueuedCommand]:
        return list(self._items)

    def __len__(self) -> int:
        return len(self._items)
