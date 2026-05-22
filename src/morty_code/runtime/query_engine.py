from __future__ import annotations

import asyncio
import inspect
from datetime import datetime
from typing import Callable
from uuid import uuid4

from morty_code.compact.auto_compact import AutoCompactDecider
from morty_code.compact.compact_agent import CompactAgent
from morty_code.compact.compact_rebuild import (
    build_reinjection_attachments,
    clone_retained_messages_for_compact,
    rebuild_post_compact_messages,
)
from morty_code.agents.task_notifications import drain_task_notifications
from morty_code.memory.durable_memory import DurableMemoryStore
from morty_code.memory.memory_extractor import MemoryExtractor
from morty_code.memory.session_memory import SessionMemoryStore
from morty_code.runtime.queue_manager import QueueManager
from morty_code.transcript.conversation_recovery import ConversationRecovery
from morty_code.transcript.session_restore import SessionRestore
from morty_code.types.messages import Message
from morty_code.types.runtime_state import CacheSafeParams, QueuedCommand, ToolUseContext


class QueryEngine:
    """顶层入口，负责把输入层、prompt 层、query loop 串起来。"""

    def __init__(
        self,
        prompt_builder,
        input_dispatcher,
        input_processor,
        query_loop,
        transcript_store,
        auto_compact_decider: AutoCompactDecider | None = None,
        compact_agent: CompactAgent | None = None,
        memory_extractor: MemoryExtractor | None = None,
        memory_write_char_threshold: int = 12000,
    ) -> None:
        """初始化对象状态。"""
        self.prompt_builder = prompt_builder
        self.input_dispatcher = input_dispatcher
        self.input_processor = input_processor
        self.query_loop = query_loop
        self.transcript_store = transcript_store
        self.auto_compact_decider = auto_compact_decider or AutoCompactDecider(
            token_threshold=12000
        )
        self.compact_agent = compact_agent or CompactAgent()
        self.memory_extractor = memory_extractor or MemoryExtractor()
        self.memory_write_char_threshold = memory_write_char_threshold
        self.messages: list[Message] = []
        self.queue_manager = QueueManager()
        self.recovery = ConversationRecovery()
        self.session_restore = SessionRestore()

    async def submit_message(
        self,
        raw_input: str,
        tool_context: ToolUseContext,
        pasted_contents: dict[int, dict[str, object]] | None = None,
        on_new_messages: Callable[[list[Message]], None] | None = None,
    ) -> list[Message]:
        # 输入层可能把一条用户输入拆成多条队列命令：普通 prompt、slash
        # command、由 slash command 生成的 follow-up prompt 都走同一条管线。
        # 这样 transcript 中看到的是统一消息流，而不是多个旁路入口。
        """提交用户输入并驱动一次处理。"""
        queued_commands = await self.input_dispatcher.submit(
            raw_input=raw_input,
            mode="prompt",
            pasted_contents=pasted_contents,
        )
        return await self._submit_queued_commands(
            raw_input=raw_input,
            queued_commands=queued_commands,
            tool_context=tool_context,
            on_new_messages=on_new_messages,
        )

    async def submit_pending_notifications(
        self,
        tool_context: ToolUseContext,
        on_new_messages: Callable[[list[Message]], None] | None = None,
    ) -> list[Message]:
        """处理后台任务通知，不需要伪造用户输入。"""

        queued_commands = drain_task_notifications(tool_context.app_state)
        if not queued_commands:
            return []
        return await self._submit_queued_commands(
            raw_input="",
            queued_commands=queued_commands,
            tool_context=tool_context,
            on_new_messages=on_new_messages,
        )

    async def _submit_queued_commands(
        self,
        *,
        raw_input: str,
        queued_commands: list[QueuedCommand],
        tool_context: ToolUseContext,
        on_new_messages: Callable[[list[Message]], None] | None = None,
    ) -> list[Message]:
        """提交已经归一化的队列命令，统一处理用户输入和后台通知。"""

        self.queue_manager.extend(queued_commands)
        queued_commands = self.queue_manager.drain()
        await self.transcript_store.append_event(
            {
                "type": "turn_start",
                "raw_input_chars": len(raw_input),
                "queued_count": len(queued_commands),
                "message_count_before": len(self.messages),
            }
        )

        new_messages: list[Message] = []
        should_query = False
        should_compact = False
        scoped_tools: list[str] | None = None
        index = 0
        while queued_commands:
            command = queued_commands.pop(0)
            # 只有第一条命令注入 @file、memory 等输入附件。后续由系统生成的
            # next_input 不重复注入，避免同一附件在一轮里膨胀多次。
            processed = await self.input_processor.process(
                command=command,
                context=tool_context,
                messages=self.messages,
                skip_attachments=index > 0,
            )
            new_messages.extend(processed.messages)
            if index == 0:
                should_query = processed.should_query
            should_compact = should_compact or processed.trigger_compact
            if processed.allowed_tools is not None:
                scoped_tools = processed.allowed_tools
            if processed.model is not None:
                tool_context.model = processed.model
            if processed.next_input is not None:
                # slash command 可以把用户输入改写成下一条 prompt，例如 /compact
                # 或 plan mode 的隐式指令。这里重新入队，保持执行顺序可恢复。
                self.queue_manager.enqueue(
                    type(command)(
                        value=processed.next_input,
                        mode="prompt",
                        skip_slash_commands=True,
                        is_meta=True,
                        origin={"source": "processed_next_input"},
                    )
                )
                queued_commands.extend(self.queue_manager.drain())
            index += 1

        self.messages.extend(new_messages)
        if new_messages:
            await self.transcript_store.append_messages(new_messages)
        decision_scoped_tools = self._maybe_apply_decision_gate(
            raw_input=raw_input,
            tool_context=tool_context,
            should_query=should_query,
        )
        if decision_scoped_tools is not None:
            scoped_tools = decision_scoped_tools
            # decision gate 注入的 meta 指令同样要进入 transcript；否则恢复
            # session 后模型看不到上一轮为什么只给方案。
            await self.transcript_store.append_messages([self.messages[-1]])
        if should_compact:
            # 手动 compact 是本地状态迁移，不需要再请求模型普通回答；成功后
            # 直接返回 compact 产生的 summary/reinjection 消息。
            compact_messages = await self._maybe_compact(tool_context, force=True, trigger="manual")
            if compact_messages:
                await self.transcript_store.append_event(
                    {
                        "type": "turn_finish",
                        "queried_model": False,
                        "input_message_count": len(new_messages),
                        "output_message_count": len(compact_messages),
                        "message_count_after": len(self.messages),
                    }
                )
                return [*new_messages, *compact_messages]
        if not should_query:
            await self.transcript_store.append_event(
                {
                    "type": "turn_finish",
                    "queried_model": False,
                    "input_message_count": len(new_messages),
                    "output_message_count": 0,
                    "message_count_after": len(self.messages),
                }
            )
            return new_messages

        await self._maybe_compact(tool_context)
        # compact 后只发送 boundary 之后的上下文，避免已经被摘要覆盖的旧消息
        # 再次进入 prompt，造成 token 浪费或工具配对重复。
        messages_for_query = self._messages_after_compact_boundary()
        original_tools = list(tool_context.tools)
        try:
            if scoped_tools is not None:
                # Slash command 的 allowed_tools 只约束当前生成的 prompt，不能永久
                # 改写 session 工具集；否则 /memory、/compact 这类空工具命令会让
                # 后续普通用户输入全部变成 tool unavailable。
                tool_context.tools = scoped_tools
            system_prompt, user_context, system_context = await self.prompt_builder.build_for_context(
                tool_context
            )
            # QueryLoop 会在模型/工具之间多轮往返；on_new_messages 用于 CLI
            # 实时打印，不改变 transcript 的最终 append-only 语义。
            result = await self.query_loop.run(
                messages=messages_for_query,
                cache_safe=CacheSafeParams(
                    system_prompt=system_prompt,
                    user_context=user_context,
                    system_context=system_context,
                    messages=list(messages_for_query),
                ),
                tool_context=tool_context,
                on_new_messages=on_new_messages,
            )
        except Exception as exc:  # noqa: BLE001 - 顶层兜底，保证 transcript 不因异常断链。
            error_message = self._assistant_error_message(str(exc))
            self.messages.append(error_message)
            await self.transcript_store.append_event(
                {
                    "type": "turn_failed",
                    "error": str(exc),
                    "message_count_before_failure": len(self.messages),
                }
            )
            await self.transcript_store.append_messages([error_message])
            return [error_message]
        finally:
            tool_context.tools = original_tools
        self.messages.extend(result.new_messages)
        # metadata event 与 message 分开追加：event 用于诊断和恢复辅助，不会
        # 进入下一轮模型上下文。
        for event in result.metadata_events:
            await self.transcript_store.append_event(event)
        if result.new_messages:
            await self.transcript_store.append_messages(result.new_messages)
            await self._maybe_write_memories_async(
                tool_context,
                result.new_messages,
                raw_input=raw_input,
            )
        await self.transcript_store.append_event(
            {
                "type": "turn_finish",
                "queried_model": True,
                "input_message_count": len(new_messages),
                "output_message_count": len(result.new_messages),
                "message_count_after": len(self.messages),
            }
        )
        return result.new_messages

    def _maybe_apply_decision_gate(
        self,
        *,
        raw_input: str,
        tool_context: ToolUseContext,
        should_query: bool,
    ) -> list[str] | None:
        """复杂任务先让模型给方案，等用户选择后再恢复工具执行。"""

        if not should_query or not raw_input.strip() or raw_input.lstrip().startswith("/"):
            return None
        mode = str(tool_context.app_state.get("decision_gate") or "auto")
        if mode == "off":
            return None
        if "enter_plan_mode" in tool_context.tools:
            return None
        pending = tool_context.app_state.get("decision_gate_pending")
        if isinstance(pending, dict):
            if _looks_like_decision_choice(raw_input):
                tool_context.app_state.pop("decision_gate_pending", None)
            return None
        if mode != "always" and not _looks_like_complex_request(raw_input):
            return None
        instruction = Message(
            uuid=str(uuid4()),
            timestamp=datetime.utcnow().isoformat(),
            type="user",
            payload={
                "content": (
                    "当前用户请求可能存在多种实现方案。不要调用任何工具，不要修改文件，"
                    "不要运行有副作用工具。"
                    "请先给出 2-3 个可选方案，每个方案说明改动范围、优点、风险和推荐程度。"
                    "最后明确询问用户选择哪个方案；在用户选择前不要开始实现。"
                )
            },
            is_meta=True,
            origin={"source": "decision_gate"},
        )
        self.messages.append(instruction)
        tool_context.app_state["decision_gate_pending"] = {
            "status": "awaiting_choice",
            "request": raw_input,
        }
        return []

    def submit_message_sync(
        self,
        raw_input: str,
        tool_context: ToolUseContext,
        pasted_contents: dict[int, dict[str, object]] | None = None,
        on_new_messages: Callable[[list[Message]], None] | None = None,
    ) -> list[Message]:
        """提交用户输入并驱动一次处理。"""
        return asyncio.run(
            self.submit_message(
                raw_input,
                tool_context,
                pasted_contents,
                on_new_messages=on_new_messages,
            )
        )

    def submit_pending_notifications_sync(
        self,
        tool_context: ToolUseContext,
        on_new_messages: Callable[[list[Message]], None] | None = None,
    ) -> list[Message]:
        """同步处理后台任务通知，供 CLI 空闲 pump 调用。"""
        return asyncio.run(
            self.submit_pending_notifications(
                tool_context,
                on_new_messages=on_new_messages,
            )
        )

    async def restore_from_transcript(self, metadata: dict[str, object] | None = None) -> dict[str, object]:
        """从历史记录恢复运行状态。"""
        loaded = await self.transcript_store.load_session()
        recovered_messages = self.recovery.recover(loaded.messages)
        restored = self.session_restore.restore(
            recovered_messages,
            metadata or {"cwd": ".", "model": "echo-model"},
        )
        self.session_restore.restore_content_replacement_events(
            loaded.metadata_events,
            restored["tool_context"].content_replacement_state,
        )
        self.messages = recovered_messages
        self.transcript_store._last_parent_uuid = loaded.last_parent_uuid
        return restored

    async def _maybe_compact(
        self,
        tool_context: ToolUseContext,
        force: bool = False,
        trigger: str = "auto",
    ) -> list[Message]:
        """内部按条件执行可选处理。"""
        approximate_tokens = sum(len(str(message.payload)) for message in self.messages)
        if not force and not self.auto_compact_decider.should_compact(approximate_tokens):
            return []
        try:
            summary_messages, messages_to_keep = await self.compact_agent.compact_messages(
                self.messages,
                trigger=trigger,
            )
            reinjected = build_reinjection_attachments(tool_context)
            rebuilt = rebuild_post_compact_messages(summary_messages, messages_to_keep, reinjected)
            self.messages = rebuilt
            compact_messages = [*summary_messages, *reinjected]
            transcript_messages = [
                *compact_messages,
                *clone_retained_messages_for_compact(messages_to_keep),
            ]
            await self.transcript_store.append_messages(transcript_messages)
            await self.transcript_store.append_event(
                {
                    "type": "compact",
                    "trigger": trigger,
                    "approximate_tokens": approximate_tokens,
                    "summary_count": len(summary_messages),
                    "messages_to_keep_count": len(messages_to_keep),
                    "reinjected_count": len(reinjected),
                    "retained_transcript_count": len(transcript_messages) - len(compact_messages),
                }
            )
            self.auto_compact_decider.record_success()
            return compact_messages
        except Exception as exc:  # noqa: BLE001 - compact 失败不能中断主对话。
            self.auto_compact_decider.record_failure()
            await self.transcript_store.append_event(
                {
                    "type": "compact_failed",
                    "approximate_tokens": approximate_tokens,
                    "error": str(exc),
                }
            )
            return []

    def _write_memories(self, tool_context: ToolUseContext, new_messages: list[Message]) -> None:
        """内部写入持久化内容。"""
        candidates = self.memory_extractor.extract(new_messages)
        self._route_memory_candidates(tool_context, candidates)

    async def _write_memories_async(
        self,
        tool_context: ToolUseContext,
        new_messages: list[Message],
    ) -> None:
        """内部写入持久化内容。"""
        extracted = self.memory_extractor.extract(new_messages)
        candidates = await extracted if inspect.isawaitable(extracted) else extracted
        self._route_memory_candidates(tool_context, candidates)

    def _route_memory_candidates(
        self,
        tool_context: ToolUseContext,
        candidates,
    ) -> None:
        """内部处理该方法负责的业务逻辑。"""
        if not candidates:
            return
        if tool_context.session_memory_path:
            session_store = SessionMemoryStore(tool_context.session_memory_path)
            for candidate in candidates:
                if candidate.target == "session":
                    session_store.append_note(candidate.text)
        if tool_context.durable_memory_dir:
            durable_store = DurableMemoryStore(tool_context.durable_memory_dir)
            for candidate in candidates:
                if candidate.target == "durable":
                    durable_store.append_summary(
                        candidate.text,
                        memory_type=(
                            candidate.memory_type
                            or self._durable_memory_type(candidate.topic)
                        ),
                    )

    def _maybe_write_memories(
        self,
        tool_context: ToolUseContext,
        new_messages: list[Message],
        *,
        raw_input: str,
    ) -> bool:
        """内部按条件执行可选处理。"""
        if not self._should_write_memories(raw_input):
            return False
        self._write_memories(tool_context, new_messages)
        return True

    async def _maybe_write_memories_async(
        self,
        tool_context: ToolUseContext,
        new_messages: list[Message],
        *,
        raw_input: str,
    ) -> bool:
        """内部按条件执行可选处理。"""
        if not self._should_write_memories(raw_input):
            return False
        await self._write_memories_async(tool_context, new_messages)
        return True

    def _should_write_memories(self, raw_input: str) -> bool:
        """内部判断是否需要执行后续动作。"""
        if self._is_explicit_memory_request(raw_input):
            return True
        return self._approximate_message_chars(self.messages) >= self.memory_write_char_threshold

    def _is_explicit_memory_request(self, raw_input: str) -> bool:
        """内部判断当前对象是否满足条件。"""
        normalized = raw_input.strip().lower()
        if normalized.startswith("/memory"):
            return True
        return any(marker in normalized for marker in ("remember this", "记住", "以后记住"))

    def _approximate_message_chars(self, messages: list[Message]) -> int:
        """内部处理该方法负责的业务逻辑。"""
        return sum(len(str(message.payload)) for message in messages)

    def _durable_memory_type(self, topic: str) -> str:
        """内部处理该方法负责的业务逻辑。"""
        return {
            "preference": "user",
            "constraint": "project",
            "environment": "project",
            "decision": "project",
            "task": "project",
            "reference": "reference",
            "feedback": "feedback",
        }.get(topic, "project")

    def _messages_after_compact_boundary(self) -> list[Message]:
        """内部处理该方法负责的业务逻辑。"""
        for index in range(len(self.messages) - 1, -1, -1):
            message = self.messages[index]
            if message.type == "system" and message.payload.get("subtype") == "compact_boundary":
                return self.messages[index:]
        return self.messages

    def _assistant_error_message(self, content: str) -> Message:
        """内部处理该方法负责的业务逻辑。"""
        return Message(
            uuid=str(uuid4()),
            timestamp=datetime.utcnow().isoformat(),
            type="assistant",
            payload={
                "content": [{"type": "text", "text": f"Runtime error: {content}"}],
                "is_api_error": True,
            },
            is_meta=True,
        )


def _looks_like_decision_choice(text: str) -> bool:
    """判断用户是否已经在选择方案。"""

    normalized = text.strip().lower()
    if not normalized:
        return False
    choice_markers = [
        "选",
        "方案",
        "推荐",
        "按你说的",
        "直接实现",
        "go on",
        "continue",
    ]
    return any(marker in normalized for marker in choice_markers)


def _looks_like_complex_request(text: str) -> bool:
    """轻量规则：命中架构/复杂改造类意图时先进入方案选择。"""

    normalized = text.strip().lower()
    complex_markers = [
        "实现",
        "完善",
        "优化",
        "重构",
        "改造",
        "设计",
        "方案",
        "生产级",
        "更优雅",
        "一步到位",
        "支持",
        "交互",
        "架构",
        "runtime",
        "agent",
        "mcp",
        "compact",
        "permission",
        "tool",
    ]
    return any(marker in normalized for marker in complex_markers)
