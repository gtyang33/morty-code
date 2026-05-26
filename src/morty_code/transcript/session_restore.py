from __future__ import annotations

from morty_code.types.messages import Message
from morty_code.types.runtime_state import ContentReplacementState, FileViewState, ToolUseContext
from morty_code.tools.tool_result_budget import PERSISTED_OUTPUT_TAG


class SessionRestore:
    """从清洗后的 transcript 重建可继续执行的 runtime state。"""

    def restore(
        self,
        messages: list[Message],
        metadata: dict[str, object],
    ) -> dict[str, object]:
        """从历史记录恢复运行状态。"""
        read_file_state: dict[str, FileViewState] = {}
        content_replacement_state = ContentReplacementState()
        plan_state: dict[str, object] = {}
        invoked_skills: dict[str, dict[str, object]] = {}
        for message in messages:
            if message.type != "attachment":
                if message.type == "user":
                    # 恢复大 tool_result 的替换决策。这样继续会话时，同一个
                    # tool_use_id 不会从“已替换”突然变回完整大文本。
                    self._restore_replacements(message, content_replacement_state)
                continue
            if message.payload.get("attachment_type") == "at_mentioned_file":
                # @file/read_file 的可见内容会进入 read_file_state，compact 后可
                # 重新注入，multi_edit 也能判断编辑前是否读过文件。
                path = str(message.payload.get("path", ""))
                if path:
                    read_file_state[path] = FileViewState(
                        path=path,
                        content=str(message.payload.get("content", "")),
                        is_partial_view=bool(message.payload.get("truncated", False)),
                    )
            if message.payload.get("attachment_type") == "plan_mode":
                # plan mode 是状态，不只是历史消息；恢复时必须重新放入 app_state，
                # 否则重启后可能绕过“先计划后实现”的保护。
                plan_state["plan_mode"] = True
                if message.payload.get("plan_file_path"):
                    plan_state["plan_file_path"] = str(message.payload.get("plan_file_path"))
            if message.payload.get("attachment_type") in {"plan_mode_exit", "approved_plan"}:
                plan_state["plan_mode"] = False
                if message.payload.get("plan_file_path"):
                    plan_state["plan_file_path"] = str(message.payload.get("plan_file_path"))
                approved_plan = message.payload.get("approved_plan") or message.payload.get("content")
                if approved_plan:
                    plan_state["approved_plan"] = str(approved_plan)
            if message.payload.get("attachment_type") == "invoked_skills":
                # Claude Code 会在 compact/resume 后恢复已调用 skill 的全文；
                # 否则下一次 compact 会丢掉模型已经依赖的 skill 指令。
                skills = message.payload.get("skills")
                if isinstance(skills, list):
                    for skill in skills:
                        if not isinstance(skill, dict):
                            continue
                        name = str(skill.get("name") or "").strip()
                        content = str(skill.get("content") or "")
                        if not name or not content:
                            continue
                        invoked_skills[name] = {
                            "name": name,
                            "path": str(skill.get("path") or ""),
                            "content": content,
                        }
        return {
            "messages": messages,
            "metadata": metadata,
            "tool_context": ToolUseContext(
                tools=[],
                model=str(metadata.get("model", "echo-model")),
                permission_mode=str(metadata.get("permission_mode", "default")),
                app_state={
                    "cwd": metadata.get("cwd", "."),
                    "session_id": metadata.get("session_id", "default"),
                    "transcript_path": metadata.get("transcript_path", ""),
                    "plans_dir": metadata.get("plans_dir", ".morty/plans"),
                    "invoked_skills": invoked_skills,
                    **plan_state,
                },
                read_file_state=read_file_state,
                content_replacement_state=content_replacement_state,
            ),
        }

    def _restore_replacements(
        self,
        message: Message,
        state: ContentReplacementState,
    ) -> None:
        """内部从历史记录恢复运行状态。"""
        content = message.payload.get("content")
        if not isinstance(content, list):
            return
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            tool_use_id = str(block.get("tool_use_id", ""))
            result_content = block.get("content")
            if (
                tool_use_id
                and isinstance(result_content, str)
                and (
                    result_content.startswith("[Tool result ")
                    or result_content.startswith(PERSISTED_OUTPUT_TAG)
                )
            ):
                state.seen_ids.add(tool_use_id)
                state.replacements[tool_use_id] = result_content

    def restore_content_replacement_events(
        self,
        events: list[dict[str, object]],
        state: ContentReplacementState,
    ) -> None:
        # QueryLoop 的 aggregate budget 会把替换记录写成 metadata event。恢复时
        # 也要读回来，覆盖那些已经不在当前主链消息里的旧 tool_result 决策。
        """从历史记录恢复运行状态。"""
        for event in events:
            if event.get("type") != "content-replacement":
                continue
            replacements = event.get("replacements")
            if not isinstance(replacements, list):
                continue
            for record in replacements:
                if not isinstance(record, dict) or record.get("kind") != "tool-result":
                    continue
                tool_use_id = str(record.get("tool_use_id", ""))
                replacement = record.get("replacement")
                if tool_use_id and isinstance(replacement, str):
                    state.seen_ids.add(tool_use_id)
                    state.replacements[tool_use_id] = replacement
