from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from morty_code.agents.agent_definitions import AgentDefinition, AgentRegistry
from morty_code.agents.forked_agent import ForkedAgentRunner
from morty_code.tools.tool_registry import ToolRegistry
from morty_code.transcript.transcript_store import TranscriptStore
from morty_code.types.messages import Message
from morty_code.types.runtime_state import CacheSafeParams, ToolUseContext


@dataclass
class SubagentRunResult:
    status: str
    agent_id: str
    agent_type: str
    output: str
    message_count: int
    transcript_path: str | None
    metadata_events: list[dict[str, object]]


class SubagentRunner:
    """运行同步子代理。

    当前版本刻意保持同步和短轮数：工具调用结果直接回灌给父 agent，后台任务、
    worktree 和远程执行以后再补。
    """

    def __init__(
        self,
        query_loop,
        tool_registry: ToolRegistry,
        agent_registry: AgentRegistry | None = None,
    ) -> None:
        self.query_loop = query_loop
        self.tool_registry = tool_registry
        self.agent_registry = agent_registry or AgentRegistry()

    async def run(
        self,
        *,
        agent_type: str,
        prompt: str,
        parent_context: ToolUseContext,
        parent_cache_safe: CacheSafeParams,
        max_turns: int | None = None,
        agent_id: str | None = None,
        record_transcript: bool = True,
    ) -> SubagentRunResult:
        definition = self._resolve_agent(agent_type)
        resolved_agent_id = agent_id or str(uuid4())
        allowed_tools = self._resolve_allowed_tools(definition, parent_context.tools)
        agent_context = parent_context
        transcript_store = self._make_transcript_store(parent_context, resolved_agent_id)
        # ForkedAgentRunner 内部会 clone context；这里先把工具和 schema 放到父 context
        # 的临时副本输入中，由 clone 复制成子代理自己的隔离状态。
        original_tools = list(parent_context.tools)
        had_tool_schemas = "tool_schemas" in parent_context.app_state
        original_schemas = parent_context.app_state.get("tool_schemas")
        try:
            # 子代理只能看到它被允许使用的工具。比如 Explore/Plan 默认只读，
            # verification 可跑 bash，但 general-purpose 仍会继承父级权限策略。
            parent_context.tools = allowed_tools
            parent_context.app_state["tool_schemas"] = self.tool_registry.api_tool_schemas(set(allowed_tools))
            await self._append_lifecycle_event(
                transcript_store,
                {
                    "type": "subagent_start",
                    "timestamp": datetime.utcnow().isoformat(),
                    "agent_id": resolved_agent_id,
                    "agent_type": definition.agent_type,
                    "prompt_chars": len(prompt),
                    "tool_count": len(allowed_tools),
                    "max_turns": max_turns or definition.max_turns,
                },
                record_transcript,
            )
            agent_cache_safe = self._build_cache_safe(
                definition=definition,
                parent_cache_safe=parent_cache_safe,
                allowed_tools=allowed_tools,
            )
            prompt_message = self._prompt_message(prompt, definition)
            # ForkedAgentRunner 会复制 mutable state，子代理读文件、compact、
            # prompt cache 状态不会直接污染父代理；最终只返回摘要结果。
            runner = ForkedAgentRunner(
                self.query_loop,
                transcript_store=transcript_store if record_transcript else None,
            )
            result = await runner.run_with_result(
                cache_safe=agent_cache_safe,
                prompt_messages=[prompt_message],
                tool_context=agent_context,
                max_turns=max_turns or definition.max_turns,
                fork_label=f"subagent:{definition.agent_type}",
                skip_transcript=not record_transcript,
                skip_cache_write=True,
            )
        finally:
            parent_context.tools = original_tools
            if had_tool_schemas:
                parent_context.app_state["tool_schemas"] = original_schemas
            else:
                parent_context.app_state.pop("tool_schemas", None)

        output = self._extract_final_output(result.messages)
        status = "completed" if output or result.messages else "failed"
        metadata_events = [
            {
                "type": "subagent_result",
                "timestamp": datetime.utcnow().isoformat(),
                "agent_id": resolved_agent_id,
                "agent_type": definition.agent_type,
                "status": status,
                "message_count": len(result.messages),
                "tool_count": len(allowed_tools),
                "transcript_path": str(transcript_store.path) if transcript_store else None,
            },
            *result.metadata_events,
        ]
        await self._append_lifecycle_event(
            transcript_store,
            {
                "type": "subagent_finish",
                "timestamp": datetime.utcnow().isoformat(),
                "agent_id": resolved_agent_id,
                "agent_type": definition.agent_type,
                "status": status,
                "message_count": len(result.messages),
                "output_chars": len(output),
            },
            record_transcript,
        )
        return SubagentRunResult(
            status=status,
            agent_id=resolved_agent_id,
            agent_type=definition.agent_type,
            output=output,
            message_count=len(result.messages),
            transcript_path=str(transcript_store.path) if transcript_store else None,
            metadata_events=metadata_events,
        )

    def _resolve_agent(self, agent_type: str) -> AgentDefinition:
        selected = self.agent_registry.find(agent_type or "general-purpose")
        if selected is None:
            available = ", ".join(agent.agent_type for agent in self.agent_registry.list())
            raise ValueError(f"Unknown subagent_type '{agent_type}'. Available: {available}")
        return selected

    def _resolve_allowed_tools(
        self,
        definition: AgentDefinition,
        parent_tools: list[str],
    ) -> list[str]:
        if definition.tools == ["*"]:
            allowed = list(parent_tools)
        else:
            allowed = [tool for tool in definition.tools if tool in parent_tools]
        blocked = set(definition.disallowed_tools)
        # 第一版禁止递归 spawn，避免模型无上限地产生代理树。
        blocked.add("spawn_agent")
        return [tool for tool in allowed if tool not in blocked]

    def _build_cache_safe(
        self,
        *,
        definition: AgentDefinition,
        parent_cache_safe: CacheSafeParams,
        allowed_tools: list[str],
    ) -> CacheSafeParams:
        system_context = dict(parent_cache_safe.system_context)
        system_context["tool_schemas_json"] = json.dumps(
            self.tool_registry.api_tool_schemas(set(allowed_tools)),
            ensure_ascii=False,
        )
        system_context["subagent_type"] = definition.agent_type
        return CacheSafeParams(
            system_prompt=[definition.system_prompt],
            user_context=dict(parent_cache_safe.user_context),
            system_context=system_context,
            messages=[],
        )

    def _prompt_message(self, prompt: str, definition: AgentDefinition) -> Message:
        content = prompt
        if definition.readonly:
            content = f"{prompt}\n\nReminder: this is a read-only delegated task."
        return Message(
            uuid=str(uuid4()),
            timestamp=datetime.utcnow().isoformat(),
            type="user",
            payload={"content": content, "mode": "subagent", "agent_type": definition.agent_type},
            is_meta=True,
            origin={"source": "subagent", "agent_type": definition.agent_type},
        )

    def _make_transcript_store(
        self,
        parent_context: ToolUseContext,
        agent_id: str,
    ) -> TranscriptStore | None:
        session_id = str(parent_context.app_state.get("session_id") or "default")
        root = Path(str(parent_context.app_state.get("subagent_transcripts_dir") or ".morty/subagents"))
        path = root / session_id / f"{agent_id}.jsonl"
        return TranscriptStore(path, agent_id)

    async def _append_lifecycle_event(
        self,
        transcript_store: TranscriptStore | None,
        event: dict[str, object],
        enabled: bool,
    ) -> None:
        if not enabled or transcript_store is None:
            return
        await transcript_store.append_event(event)

    def _extract_final_output(self, messages: list[Message]) -> str:
        for message in reversed(messages):
            if message.type != "assistant":
                continue
            content = message.payload.get("content", [])
            if isinstance(content, str):
                return content
            if not isinstance(content, list):
                continue
            parts = [
                str(block.get("text", ""))
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            ]
            text = "\n".join(part for part in parts if part).strip()
            if text:
                return text
        return ""
