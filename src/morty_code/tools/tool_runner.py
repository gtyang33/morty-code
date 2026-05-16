from __future__ import annotations

from datetime import UTC, datetime
from inspect import isawaitable
from uuid import uuid4

from morty_code.security import PermissionDecision, evaluate_tool_permission
from morty_code.tools.schema_validation import ToolInputValidationError, validate_tool_input
from morty_code.tools.tool_result_budget import (
    DEFAULT_RESULT_BUDGET_CHARS,
    ToolResultCandidate,
    _content_size,
    _persist_and_build_replacement,
)
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
        """执行核心流程。"""
        return []


class ToolRunner:
    """执行 assistant 消息里的 tool_use block，并把结果回灌为 user 消息。

    这里复刻的是 Claude Code 的核心语义：工具结果不是旁路返回值，而是继续进入
    消息流，下一轮模型采样才能看到稳定的工具轨迹。
    """

    def __init__(self, registry: ToolRegistry) -> None:
        """初始化对象状态。"""
        self.registry = registry

    async def run(
        self,
        assistant_message: Message,
        context: ToolUseContext,
        cache_safe: CacheSafeParams | None = None,
    ) -> list[Message]:
        """执行核心流程。"""
        tool_uses = self._extract_tool_uses(assistant_message)
        results: list[dict[str, object]] = []
        for tool_use in tool_uses:
            name = str(tool_use.get("name", ""))
            tool_use_id = str(tool_use.get("id") or "")
            tool = self.registry.find(name)
            # 模型可能幻觉工具名，或者当前 permission/plan mode 临时裁剪了工具集。
            # 这种情况不能抛异常中断对话，而是回灌一个 tool_result，让模型自我修正。
            if tool is None or name not in context.tools:
                self._record_tool_event(
                    context,
                    {
                        "phase": "unavailable",
                        "tool_name": name,
                        "tool_use_id": tool_use_id,
                    },
                )
                results.append(
                    self._tool_result(
                        tool_use,
                        content=f"Tool '{name}' is not available.",
                        is_error=True,
                    )
                )
                continue
            tool_input = dict(tool_use.get("input") or {})
            try:
                # schema 校验必须早于权限请求，否则用户可能被要求批准一个
                # 后续一定会失败的工具调用。
                validate_tool_input(name, tool.input_schema, tool_input)
            except ToolInputValidationError as exc:
                self._record_tool_event(
                    context,
                    {
                        "phase": "validation_error",
                        "tool_name": name,
                        "tool_use_id": tool_use_id,
                        "error": str(exc),
                    },
                )
                results.append(
                    self._tool_result(
                        tool_use,
                        content=f"<tool_use_error>{exc}</tool_use_error>",
                        is_error=True,
                    )
                )
                continue
            decision = evaluate_tool_permission(name, tool_input, context)
            # 权限决策也作为 metadata event 记录，方便事后解释“为什么工具没执行”。
            self._record_tool_event(
                context,
                {
                    "phase": "permission",
                    "tool_name": name,
                    "tool_use_id": tool_use_id,
                    "behavior": decision.behavior,
                    "reason": decision.reason,
                    "message": decision.message,
                },
            )
            if decision.behavior == "ask":
                # harness/外部 UI 可以在批准时改写 input，例如修正 bash 命令或
                # 限制文件路径；ToolRunner 必须使用批准后的 updated_input。
                decision = await self._request_external_permission(
                    tool_name=name,
                    tool_input=tool_input,
                    tool_use=tool_use,
                    decision=decision,
                    context=context,
                )
                if decision.updated_input is not None:
                    tool_input = dict(decision.updated_input)
                self._record_tool_event(
                    context,
                    {
                        "phase": "permission",
                        "tool_name": name,
                        "tool_use_id": tool_use_id,
                        "behavior": decision.behavior,
                        "reason": decision.reason,
                        "message": decision.message,
                        "external": True,
                    },
                )
            if decision.behavior != "allow":
                self._record_tool_event(
                    context,
                    {
                        "phase": "blocked",
                        "tool_name": name,
                        "tool_use_id": tool_use_id,
                        "reason": decision.reason,
                        "message": decision.message,
                    },
                )
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
                started = datetime.now(UTC)
                self._record_tool_event(
                    context,
                    {
                        "phase": "start",
                        "tool_name": name,
                        "tool_use_id": tool_use_id,
                    },
                )
                if tool.needs_context:
                    # 需要 context 的工具可以访问 read_file_state、permission state、
                    # subagent dirs 等 runtime 状态；普通纯函数工具保持简单签名。
                    if cache_safe is None:
                        raise RuntimeError("cache_safe context is required for this tool")
                    payload = await tool.handler(tool_input, context, cache_safe)
                else:
                    payload = await tool.handler(tool_input)
                # 单个工具返回可能非常大，先在工具层做一次替换，后续 QueryLoop
                # 还会按整轮 aggregate budget 做稳定替换。
                content = self._maybe_replace_large_result(tool_use, payload, context)
                self._record_tool_event(
                    context,
                    {
                        "phase": "success",
                        "tool_name": name,
                        "tool_use_id": tool_use_id,
                        "duration_ms": int((datetime.now(UTC) - started).total_seconds() * 1000),
                    },
                )
                results.append(self._tool_result(tool_use, content=content, is_error=False))
            except Exception as exc:  # noqa: BLE001 - 工具异常必须进入 transcript，不能丢失。
                self._record_tool_event(
                    context,
                    {
                        "phase": "error",
                        "tool_name": name,
                        "tool_use_id": tool_use_id,
                        "error": str(exc),
                    },
                )
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
        """内部处理该方法负责的业务逻辑。"""
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
        """内部提取后续流程需要的信息。"""
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
        """内部处理该方法负责的业务逻辑。"""
        return {
            "type": "tool_result",
            "tool_use_id": tool_use.get("id", ""),
            "content": content,
            "is_error": is_error,
        }

    def _record_tool_event(
        self,
        context: ToolUseContext,
        event: dict[str, object],
    ) -> None:
        """内部记录运行状态或诊断事件。"""
        events = context.app_state.setdefault("tool_execution_events", [])
        if not isinstance(events, list):
            return
        events.append(
            {
                "type": "tool_execution",
                "timestamp": datetime.now(UTC).isoformat(),
                **event,
            }
        )

    def _maybe_replace_large_result(
        self,
        tool_use: dict[str, object],
        payload: object,
        context: ToolUseContext,
    ) -> object:
        """内部按条件执行可选处理。"""
        tool_use_id = str(tool_use.get("id", ""))
        max_chars = int(context.app_state.get("tool_result_max_chars", DEFAULT_RESULT_BUDGET_CHARS))
        if not tool_use_id:
            return payload
        existing = context.content_replacement_state.replacements.get(tool_use_id)
        if existing is not None:
            return existing
        if _content_size(payload) <= max_chars:
            return payload
        replacement = _persist_and_build_replacement(
            candidate=ToolResultCandidate(
                tool_use_id=tool_use_id,
                content=payload,
                size=_content_size(payload),
            ),
            tool_results_dir=context.app_state.get("tool_results_dir", ".morty/tool-results"),
        )
        # 只记录一次替换决策，resume/fork 可以复用相同 persisted-output 文本。
        context.content_replacement_state.seen_ids.add(tool_use_id)
        context.content_replacement_state.replacements[tool_use_id] = replacement
        return replacement
