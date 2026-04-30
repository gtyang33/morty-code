from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path
from uuid import uuid4

from morty_code.memory.relevant_memory import RelevantMemoryFinder
from morty_code.types.messages import Attachment, AttachmentPhase, Message
from morty_code.types.runtime_state import FileViewState, QueuedCommand, ToolUseContext


AT_MENTION_RE = re.compile(r"@([A-Za-z0-9_./-]+)")
TURNS_BETWEEN_MODE_ATTACHMENTS = 3
DEFAULT_MAX_ATTACHMENTS_PER_TURN = 20
DEFAULT_MAX_ATTACHMENT_CHARS = 20000


class AttachmentManager:
    """负责首轮同步附件与轮尾增量附件。"""

    def __init__(self, relevant_memory_finder: RelevantMemoryFinder | None = None) -> None:
        self.relevant_memory_finder = relevant_memory_finder

    @classmethod
    def from_context(cls, context: ToolUseContext) -> "AttachmentManager":
        finder = None
        if context.durable_memory_dir:
            finder = RelevantMemoryFinder(context.durable_memory_dir)
        return cls(relevant_memory_finder=finder)

    async def collect_initial(
        self,
        input_text: str,
        context: ToolUseContext,
        messages: list[Message],
    ) -> list[Attachment]:
        attachments: list[Attachment] = []
        for match in AT_MENTION_RE.finditer(input_text):
            attachments.append(self._build_at_mentioned_attachment(match.group(1), context))
        if self.relevant_memory_finder is not None:
            attachments.extend(self._tag_relevant_memories(self.relevant_memory_finder.find(input_text)))
        return self._finalize_attachments(
            attachments,
            context=context,
            messages=messages,
            phase="input",
            allow_seen_stable_keys=True,
        )

    async def collect_post_iteration(
        self,
        input_text: str,
        context: ToolUseContext,
        messages: list[Message],
        queued_commands: list[QueuedCommand],
    ) -> list[Attachment]:
        attachments: list[Attachment] = []
        attachments.extend(self._collect_date_change(context))
        attachments.extend(self._collect_mode_reminders(context))
        attachments.extend(self._collect_hook_context(context))
        for command in queued_commands:
            if command.mode not in {"prompt", "task-notification"}:
                continue
            attachments.append(
                Attachment(
                    type="queued_command",
                    payload={
                        "prompt": command.value,
                        "mode": command.mode,
                    },
                    source_uuid=command.uuid,
                    is_meta=command.is_meta,
                    phase="delta",
                    stable_key=f"delta:queued_command:{command.uuid or command.value}",
                )
            )
        return self._finalize_attachments(
            attachments,
            context=context,
            messages=messages,
            phase="delta",
            allow_seen_stable_keys=False,
        )

    def collect_reinjection(
        self,
        context: ToolUseContext,
        messages: list[Message],
    ) -> list[Attachment]:
        """compact 后重新注入弱持久化状态。

        这一层和 input/delta 共享预算、去重、稳定 key，避免 compact 模块
        自己拼 attachment payload，最后变成第二套动态上下文协议。
        """

        attachments: list[Attachment] = []
        for file_state in context.read_file_state.values():
            attachments.append(
                Attachment(
                    type="at_mentioned_file",
                    payload={
                        "path": file_state.path,
                        "resolved_path": file_state.path,
                        "kind": "file",
                        "content": file_state.content,
                        "truncated": file_state.is_partial_view,
                        "source": "post_compact_reinject",
                    },
                    is_meta=True,
                    phase="reinjection",
                    stable_key=f"reinjection:file:{file_state.path}",
                )
            )
        if context.session_memory_path:
            session_path = Path(context.session_memory_path)
            if session_path.exists():
                attachments.append(
                    Attachment(
                        type="session_memory",
                        payload={
                            "path": str(session_path),
                            "content": session_path.read_text(encoding="utf-8", errors="replace"),
                            "source": "post_compact_reinject",
                        },
                        is_meta=True,
                        phase="reinjection",
                        stable_key=f"reinjection:session_memory:{session_path}",
                    )
                )
        if context.app_state.get("plan_mode"):
            attachments.append(
                Attachment(
                    type="plan_mode",
                    payload={
                        "content": "Plan mode is active after compaction. Do not modify files until the plan is approved.",
                        "source": "post_compact_reinject",
                    },
                    is_meta=True,
                    phase="reinjection",
                    stable_key="reinjection:plan_mode",
                )
            )
        if context.discovered_skill_names:
            attachments.append(
                Attachment(
                    type="skill_discovery",
                    payload={
                        "skills": sorted(context.discovered_skill_names),
                        "source": "post_compact_reinject",
                    },
                    is_meta=True,
                    phase="reinjection",
                    stable_key="reinjection:skill_discovery",
                )
            )
        tool_schemas = context.app_state.get("tool_schemas")
        if tool_schemas:
            attachments.append(
                Attachment(
                    type="command_permissions",
                    payload={
                        "allowed_tools": context.tools,
                        "tool_schema_count": len(tool_schemas) if isinstance(tool_schemas, list) else 0,
                        "source": "post_compact_reinject",
                    },
                    is_meta=True,
                    phase="reinjection",
                    stable_key="reinjection:tool_schema_summary",
                )
            )
        if context.content_replacement_state.replacements:
            attachments.append(
                Attachment(
                    type="content_replacement_state",
                    payload={
                        "replacement_ids": sorted(context.content_replacement_state.replacements),
                        "source": "post_compact_reinject",
                    },
                    is_meta=True,
                    phase="reinjection",
                    stable_key="reinjection:content_replacement_state",
                )
            )
        return self._finalize_attachments(
            attachments,
            context=context,
            messages=messages,
            phase="reinjection",
            allow_seen_stable_keys=True,
        )

    def to_message(
        self,
        attachment: Attachment,
        timestamp: str | None = None,
        origin: dict[str, object] | None = None,
    ) -> Message:
        return Message(
            uuid=str(uuid4()),
            timestamp=timestamp or datetime.utcnow().isoformat(),
            type="attachment",
            payload={
                "attachment_type": attachment.type,
                "attachment_phase": attachment.phase,
                "stable_key": attachment.stable_key,
                **attachment.payload,
            },
            is_meta=attachment.is_meta,
            origin=origin,
        )

    def bind_context(self, context: ToolUseContext) -> None:
        """根据 runtime context 延迟绑定 memory finder。

        QueryEngine 创建时不一定已经拿到 ToolUseContext，所以这里允许在每轮输入前
        根据 durable_memory_dir 补齐检索器，避免调用方必须手动重建整个处理器。
        """

        if self.relevant_memory_finder is None and context.durable_memory_dir:
            self.relevant_memory_finder = RelevantMemoryFinder(context.durable_memory_dir)

    def _build_at_mentioned_attachment(
        self,
        raw_path: str,
        context: ToolUseContext,
    ) -> Attachment:
        cwd = Path(str(context.app_state.get("cwd", "."))).expanduser()
        path = Path(raw_path).expanduser()
        resolved = path if path.is_absolute() else cwd / path

        payload: dict[str, object] = {"path": raw_path, "resolved_path": str(resolved)}
        try:
            if resolved.is_dir():
                entries = sorted(child.name + ("/" if child.is_dir() else "") for child in resolved.iterdir())
                payload.update(
                    {
                        "kind": "directory",
                        "content": "\n".join(entries[:200]),
                        "truncated": len(entries) > 200,
                    }
                )
            elif resolved.is_file():
                content = resolved.read_text(encoding="utf-8", errors="replace")
                max_chars = int(context.app_state.get("attachment_max_chars", 20000))
                truncated = len(content) > max_chars
                visible_content = content[:max_chars]
                payload.update(
                    {
                        "kind": "file",
                        "content": visible_content,
                        "truncated": truncated,
                    }
                )
                # read_file_state 表示“模型已经见过的视图”，resume 时可重建现场。
                context.read_file_state[str(resolved)] = FileViewState(
                    path=str(resolved),
                    content=visible_content,
                    is_partial_view=truncated,
                    offset=0,
                    limit=max_chars if truncated else None,
                )
            else:
                payload.update({"kind": "missing", "content": "文件不存在。"})
        except OSError as exc:
            payload.update({"kind": "error", "content": f"读取失败: {exc}"})

        return Attachment(type="at_mentioned_file", payload=payload)

    def _collect_date_change(self, context: ToolUseContext) -> list[Attachment]:
        current_date = date.today().isoformat()
        previous_date = context.app_state.get("last_attachment_date")
        context.app_state["last_attachment_date"] = current_date
        if previous_date is None or previous_date == current_date:
            return []
        return [
            Attachment(
                type="date_change",
                payload={"previous_date": previous_date, "current_date": current_date},
                is_meta=True,
                phase="delta",
                stable_key=f"delta:date_change:{current_date}",
            )
        ]

    def _collect_mode_reminders(self, context: ToolUseContext) -> list[Attachment]:
        turn_index = int(context.app_state.get("turn_index", 0)) + 1
        context.app_state["turn_index"] = turn_index
        plan_mode = bool(context.app_state.get("plan_mode", False))
        if not plan_mode:
            if context.app_state.pop("needs_plan_mode_exit_attachment", False):
                return [
                    Attachment(
                        type="plan_mode_exit",
                        payload={"content": "Plan mode has been exited. Implementation is allowed."},
                        is_meta=True,
                        phase="delta",
                        stable_key=f"delta:plan_mode_exit:{turn_index}",
                    )
                ]
            return []
        last_sent = int(context.app_state.get("last_plan_mode_attachment_turn", 0))
        if last_sent and turn_index - last_sent < TURNS_BETWEEN_MODE_ATTACHMENTS:
            return []
        context.app_state["last_plan_mode_attachment_turn"] = turn_index
        return [
            Attachment(
                type="plan_mode",
                payload={
                    "content": "Plan mode is active. Do not modify files until the plan is approved.",
                    "turn_index": turn_index,
                },
                is_meta=True,
                phase="delta",
                stable_key="delta:plan_mode",
            )
        ]

    def _collect_hook_context(self, context: ToolUseContext) -> list[Attachment]:
        queue = context.app_state.get("hook_context_queue")
        if not isinstance(queue, list) or not queue:
            return []
        context.app_state["hook_context_queue"] = []
        return [
            Attachment(
                type="hook_additional_context",
                payload={"content": str(item)},
                is_meta=True,
                phase="delta",
                stable_key=f"delta:hook_additional_context:{index}:{str(item)[:80]}",
            )
            for index, item in enumerate(queue)
        ]

    def _tag_relevant_memories(self, attachments: list[Attachment]) -> list[Attachment]:
        tagged: list[Attachment] = []
        for attachment in attachments:
            attachment.phase = "input"
            attachment.stable_key = f"input:relevant_memory:{attachment.payload.get('path', '')}"
            tagged.append(attachment)
        return tagged

    def _finalize_attachments(
        self,
        attachments: list[Attachment],
        context: ToolUseContext,
        messages: list[Message],
        phase: AttachmentPhase,
        allow_seen_stable_keys: bool,
    ) -> list[Attachment]:
        max_count = int(context.app_state.get("max_attachments_per_turn", DEFAULT_MAX_ATTACHMENTS_PER_TURN))
        max_chars = int(context.app_state.get("max_attachment_chars", DEFAULT_MAX_ATTACHMENT_CHARS))
        seen = set() if allow_seen_stable_keys else self._seen_stable_keys(messages)
        result: list[Attachment] = []
        dropped = 0
        for attachment in attachments:
            attachment.phase = attachment.phase or phase
            attachment.stable_key = attachment.stable_key or self._stable_key_for(attachment)
            if attachment.stable_key in seen:
                dropped += 1
                continue
            seen.add(attachment.stable_key)
            result.append(self._apply_budget(attachment, max_chars))
            if len(result) >= max_count:
                dropped += max(0, len(attachments) - len(result))
                break
        if dropped:
            context.app_state["last_attachment_finalize_dropped"] = dropped
        return result

    def _apply_budget(self, attachment: Attachment, max_chars: int) -> Attachment:
        content = attachment.payload.get("content")
        if not isinstance(content, str) or len(content) <= max_chars:
            return attachment
        updated_payload = dict(attachment.payload)
        updated_payload["content"] = content[:max_chars]
        updated_payload["truncated_by_budget"] = True
        updated_payload["original_chars"] = len(content)
        return Attachment(
            type=attachment.type,
            payload=updated_payload,
            source_uuid=attachment.source_uuid,
            is_meta=attachment.is_meta,
            phase=attachment.phase,
            stable_key=attachment.stable_key,
        )

    def _seen_stable_keys(self, messages: list[Message]) -> set[str]:
        return {
            str(message.payload.get("stable_key"))
            for message in messages
            if message.type == "attachment" and message.payload.get("stable_key")
        }

    def _stable_key_for(self, attachment: Attachment) -> str:
        path = attachment.payload.get("resolved_path") or attachment.payload.get("path")
        if path:
            return f"{attachment.phase}:{attachment.type}:{path}"
        return f"{attachment.phase}:{attachment.type}:{attachment.source_uuid or str(attachment.payload)[:120]}"
