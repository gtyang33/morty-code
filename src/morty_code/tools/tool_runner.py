from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from morty_code.tools.tool_registry import ToolRegistry
from morty_code.types.messages import Message
from morty_code.types.runtime_state import ToolUseContext


class NullToolRunner:
    """第一阶段空工具执行器。"""

    async def run(
        self,
        assistant_message: Message,
        context: ToolUseContext,
    ) -> list[Message]:
        return []


class ToolRunner:
    """执行 assistant 消息里的 tool_use block，并把结果回灌为 user 消息。

    这里复刻的是 Claude Code 的核心语义：工具结果不是旁路返回值，而是继续进入
    消息流，下一轮模型采样才能看到稳定的工具轨迹。
    """

    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

    async def run(
        self,
        assistant_message: Message,
        context: ToolUseContext,
    ) -> list[Message]:
        tool_uses = self._extract_tool_uses(assistant_message)
        results: list[dict[str, object]] = []
        for tool_use in tool_uses:
            name = str(tool_use.get("name", ""))
            tool = self.registry.find(name)
            if tool is None or name not in context.tools:
                results.append(
                    self._tool_result(
                        tool_use,
                        content=f"Tool '{name}' is not available.",
                        is_error=True,
                    )
                )
                continue
            try:
                payload = await tool.handler(dict(tool_use.get("input") or {}))
                content = self._maybe_replace_large_result(tool_use, payload, context)
                results.append(self._tool_result(tool_use, content=content, is_error=False))
            except Exception as exc:  # noqa: BLE001 - 工具异常必须进入 transcript，不能丢失。
                results.append(
                    self._tool_result(
                        tool_use,
                        content=f"Tool '{name}' failed: {exc}",
                        is_error=True,
                    )
                )
        if not results:
            return []
        return [
            Message(
                uuid=str(uuid4()),
                timestamp=datetime.utcnow().isoformat(),
                type="user",
                payload={"content": results},
                is_meta=True,
            )
        ]

    def _extract_tool_uses(self, assistant_message: Message) -> list[dict[str, object]]:
        content = assistant_message.payload.get("content", [])
        if not isinstance(content, list):
            return []
        return [
            block
            for block in content
            if isinstance(block, dict) and block.get("type") == "tool_use"
        ]

    def _tool_result(
        self,
        tool_use: dict[str, object],
        content: object,
        is_error: bool,
    ) -> dict[str, object]:
        return {
            "type": "tool_result",
            "tool_use_id": tool_use.get("id", ""),
            "content": content,
            "is_error": is_error,
        }

    def _maybe_replace_large_result(
        self,
        tool_use: dict[str, object],
        payload: object,
        context: ToolUseContext,
    ) -> object:
        tool_use_id = str(tool_use.get("id", ""))
        max_chars = int(context.app_state.get("tool_result_max_chars", 12000))
        serialized = str(payload)
        if not tool_use_id or len(serialized) <= max_chars:
            return payload
        replacement = (
            f"[Tool result {tool_use_id} was {len(serialized)} chars and was "
            "replaced to keep prompt size stable.]"
        )
        # 只记录一次替换决策，后续 resume/fork 可以复用相同 placeholder。
        context.content_replacement_state.seen_ids.add(tool_use_id)
        context.content_replacement_state.replacements[tool_use_id] = replacement
        return replacement
