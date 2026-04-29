from __future__ import annotations

import re
from pathlib import Path

from morty_code.memory.relevant_memory import RelevantMemoryFinder
from morty_code.types.messages import Attachment, Message
from morty_code.types.runtime_state import FileViewState, QueuedCommand, ToolUseContext


AT_MENTION_RE = re.compile(r"@([A-Za-z0-9_./-]+)")


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
            attachments.extend(self.relevant_memory_finder.find(input_text))
        return attachments

    async def collect_post_iteration(
        self,
        input_text: str,
        context: ToolUseContext,
        messages: list[Message],
        queued_commands: list[QueuedCommand],
    ) -> list[Attachment]:
        attachments: list[Attachment] = []
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
                )
            )
        return attachments

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
