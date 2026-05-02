from __future__ import annotations

from datetime import UTC, datetime
from inspect import isawaitable
from uuid import uuid4

from morty_code.security import PermissionDecision, evaluate_tool_permission
from morty_code.tools.tool_registry import ToolRegistry
from morty_code.types.messages import Message
from morty_code.types.runtime_state import CacheSafeParams, ToolUseContext


class NullToolRunner:
    """不启用工具时使用的空工具执行器。"""

    async def run(
        self,
        assistant_message: Message,
        context: ToolUseContext,
        cache_safe: CacheSafeParams | None = None,
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
        cache_safe: CacheSafeParams | None = None,
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
            tool_input = dict(tool_use.get("input") or {})
            decision = evaluate_tool_permission(name, tool_input, context)
            if decision.behavior == "ask":
                decision = await self._request_external_permission(
                    tool_name=name,
                    tool_input=tool_input,
                    tool_use=tool_use,
                    decision=decision,
                    context=context,
                )
                if decision.updated_input is not None:
                    tool_input = dict(decision.updated_input)
            if decision.behavior != "allow":
                results.append(
                    self._tool_result(
                        tool_use,
                        content=(
                            f"Tool '{name}' blocked by permission policy: "
                            f"{decision.message}"
                        ),
                        is_error=True,
                    )
                )
                continue
            try:
                if tool.needs_context:
                    if cache_safe is None:
                        raise RuntimeError("cache_safe context is required for this tool")
                    payload = await tool.handler(tool_input, context, cache_safe)
                else:
                    payload = await tool.handler(tool_input)
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
                timestamp=datetime.now(UTC).isoformat(),
                type="user",
                payload={"content": results},
                is_meta=True,
            )
        ]

    async def _request_external_permission(
        self,
        *,
        tool_name: str,
        tool_input: dict[str, object],
        tool_use: dict[str, object],
        decision: PermissionDecision,
        context: ToolUseContext,
    ) -> PermissionDecision:
        handler = context.app_state.get("permission_request_handler")
        if not callable(handler):
            return decision
        response = handler(
            {
                "tool_name": tool_name,
                "input": tool_input,
                "tool_use_id": str(tool_use.get("id") or ""),
                "decision_reason": decision.reason,
                "message": decision.message,
            }
        )
        if isawaitable(response):
            response = await response
        if not isinstance(response, dict):
            return decision
        behavior = str(response.get("behavior") or "deny")
        if behavior == "allow":
            updated = response.get("updatedInput") or response.get("updated_input")
            return PermissionDecision(
                behavior="allow",
                reason="external",
                message="Tool use approved by harness.",
                updated_input=updated if isinstance(updated, dict) else None,
            )
        return PermissionDecision(
            behavior="deny",
            reason="external",
            message=str(response.get("message") or "Tool use denied by harness."),
        )

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
        # 只记录一次替换决策，resume/fork 可以复用相同 placeholder。
        context.content_replacement_state.seen_ids.add(tool_use_id)
        context.content_replacement_state.replacements[tool_use_id] = replacement
        return replacement
