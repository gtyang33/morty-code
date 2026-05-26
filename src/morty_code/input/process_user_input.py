from __future__ import annotations

from datetime import datetime
from pathlib import Path
from uuid import uuid4

from morty_code.attachments.attachment_manager import AttachmentManager
from morty_code.input.commands import CommandRegistry, CommandSpec
from morty_code.input.slash_commands import SlashCommandProcessor, parse_slash_command
from morty_code.agents.task_registry import get_subagent_task_registry
from morty_code.memory.durable_memory import DurableMemoryStore
from morty_code.mcp.config import load_mcp_server_entries, set_mcp_server_disabled
from morty_code.mcp.manager import create_mcp_tool_registry
from morty_code.plan import PlanStore
from morty_code.skills import SkillRegistry, SkillSpec, load_skill_registry
from morty_code.tools.tool_registry import ToolRegistry
from morty_code.types.messages import Attachment, Message
from morty_code.types.runtime_state import ProcessedUserInput, QueuedCommand, ToolUseContext


class UserInputProcessor:
    """进入 query 前的消息改写层。"""

    def __init__(
        self,
        attachment_manager: AttachmentManager,
        command_registry: CommandRegistry | None = None,
    ) -> None:
        """初始化对象状态。"""
        self.attachment_manager = attachment_manager
        self.command_registry = command_registry or self._build_default_commands()
        self.slash_processor = SlashCommandProcessor(self.command_registry)

    async def process(
        self,
        command: QueuedCommand,
        context: ToolUseContext,
        messages: list[Message],
        skip_attachments: bool = False,
    ) -> ProcessedUserInput:
        """处理输入并生成标准结果。"""
        self.attachment_manager.bind_context(context)
        text = command.value if isinstance(command.value, str) else ""
        if text.startswith("/") and not command.skip_slash_commands:
            processed = await self.slash_processor.process(
                text,
                {
                    "tool_context": context,
                    "messages": messages,
                },
            )
            if processed.allowed_tools is not None:
                processed.messages.append(
                    self.attachment_manager.to_message(
                        Attachment(
                            type="command_permissions",
                            payload={
                                "command": text.split(" ", 1)[0],
                                "allowed_tools": processed.allowed_tools,
                            },
                            is_meta=True,
                            phase="input",
                            stable_key=f"input:command_permissions:{text.split(' ', 1)[0]}",
                        ),
                        timestamp=datetime.utcnow().isoformat(),
                    )
                )
            return processed

        if _is_plan_approval_input(text, context):
            exit_result = _exit_plan_mode(context)
            user_message = Message(
                uuid=command.uuid or str(uuid4()),
                timestamp=datetime.utcnow().isoformat(),
                type="user",
                payload={
                    "content": (
                        f"Plan approved. Restored permission mode: {exit_result['restore_mode']}. "
                        "Continue with implementation now.\n\n"
                        f"User request: {text.strip()}"
                    ),
                    "mode": command.mode,
                },
                is_meta=command.is_meta,
                origin=command.origin,
            )
            return ProcessedUserInput(messages=[user_message], should_query=True)

        attachments = []
        if text and not skip_attachments:
            attachments = await self.attachment_manager.collect_initial(
                input_text=text,
                context=context,
                messages=messages,
            )

        user_content: object = text
        if command.pasted_contents:
            user_content = self._build_multimodal_content(text, command.pasted_contents)

        user_message = Message(
            uuid=command.uuid or str(uuid4()),
            timestamp=datetime.utcnow().isoformat(),
            type="user",
            payload={"content": user_content, "mode": command.mode},
            is_meta=command.is_meta,
            origin=command.origin,
        )
        attachment_messages = [
            self.attachment_manager.to_message(
                attachment,
                timestamp=datetime.utcnow().isoformat(),
                origin=command.origin,
            )
            for attachment in attachments
        ]
        return ProcessedUserInput(
            messages=[user_message, *attachment_messages],
            should_query=True,
        )

    def _build_multimodal_content(
        self,
        text: str,
        pasted_contents: dict[int, dict[str, object]],
    ) -> list[dict[str, object]]:
        """把 pasted image 保留为结构化块，文本 paste 已在 InputDispatcher 展开。"""

        blocks: list[dict[str, object]] = []
        if text:
            blocks.append({"type": "text", "text": text})
        for item in pasted_contents.values():
            if item.get("type") != "image":
                continue
            blocks.append(
                {
                    "type": "image",
                    "source": item.get("source") or item.get("content") or item.get("data"),
                    "media_type": item.get("media_type", "image/png"),
                }
            )
        return blocks

    def _build_default_commands(self) -> CommandRegistry:
        """内部构建后续流程需要的数据。"""
        registry = CommandRegistry()
        registry.register(
            CommandSpec(
                name="help",
                description="显示可用命令",
                kind="local",
                handler=self._handle_help,
            )
        )
        registry.register(
            CommandSpec(
                name="status",
                description="显示当前 runtime 状态",
                kind="local",
                handler=self._handle_status,
            )
        )
        registry.register(
            CommandSpec(
                name="tools",
                description="显示当前允许的工具",
                kind="local",
                handler=self._handle_tools,
            )
        )
        registry.register(
            CommandSpec(
                name="skills",
                description="显示可用 skills",
                kind="local",
                handler=self._handle_skills,
            )
        )
        registry.register(
            CommandSpec(
                name="mcp",
                description="显示 MCP server 和 MCP tools",
                kind="local",
                handler=self._handle_mcp,
            )
        )
        registry.register(
            CommandSpec(
                name="tasks",
                description="显示后台 subagent 任务",
                kind="local",
                handler=self._handle_tasks,
            )
        )
        registry.register(
            CommandSpec(
                name="memory-index",
                description="显示 durable memory 索引",
                kind="local",
                handler=self._handle_memory_index,
            )
        )
        registry.register(
            CommandSpec(
                name="plan",
                description="进入 plan mode",
                kind="local",
                handler=self._handle_plan_mode,
            )
        )
        registry.register(
            CommandSpec(
                name="auto",
                description="批准当前计划并退出 plan mode",
                kind="local",
                handler=self._handle_auto_mode,
            )
        )
        registry.register(
            CommandSpec(
                name="plan-show",
                description="显示当前 session 的计划文件",
                kind="local",
                handler=self._handle_plan_show,
            )
        )
        registry.register(
            CommandSpec(
                name="compact",
                description="请求对话压缩",
                kind="prompt",
                handler=self._handle_compact,
                allowed_tools=[],
            )
        )
        registry.register(
            CommandSpec(
                name="memory",
                description="请求刷新会话记忆",
                kind="prompt",
                handler=self._handle_memory,
                allowed_tools=[],
            )
        )
        return registry

    async def _handle_help(self, args: str, context: dict[str, object]) -> dict[str, object]:
        """内部处理该方法负责的业务逻辑。"""
        commands = sorted(
            self.command_registry.list_user_invocable(),
            key=lambda command: command.name,
        )
        return {
            "mode": "local",
            "content": "\n".join(
                f"/{command.name} - {command.description}" for command in commands
            ),
        }

    async def _handle_tasks(self, args: str, context: dict[str, object]) -> dict[str, object]:
        """内部处理该方法负责的业务逻辑。"""
        tool_context = context["tool_context"]
        if not isinstance(tool_context, ToolUseContext):
            return {"mode": "local", "content": "Task context unavailable."}
        # /tasks 是面向用户的本地查询命令，不走模型。它读取磁盘上的 task
        # registry，所以即使 CLI 重启，也能查看历史后台子代理状态。
        task_dir = str(tool_context.app_state.get("subagent_tasks_dir") or ".morty/tasks")
        registry = get_subagent_task_registry(task_dir)
        if args.strip():
            task = registry.get(args.strip())
            if task is None:
                return {"mode": "local", "content": f"No subagent task found: {args.strip()}"}
            lines = [
                f"task_id: {task.task_id}",
                f"status: {task.status}",
                f"agent_type: {task.agent_type}",
                f"description: {task.description}",
                f"output_file: {task.output_file}",
                f"transcript_path: {task.transcript_path or 'none'}",
            ]
            if task.error:
                lines.append(f"error: {task.error}")
            if task.output:
                lines.append("")
                lines.append(task.output)
            return {"mode": "local", "content": "\n".join(lines)}
        return {"mode": "local", "content": registry.format_list()}

    async def _handle_status(self, args: str, context: dict[str, object]) -> dict[str, object]:
        """内部处理该方法负责的业务逻辑。"""
        tool_context = context["tool_context"]
        messages = context["messages"]
        if not isinstance(tool_context, ToolUseContext) or not isinstance(messages, list):
            return {"mode": "local", "content": "Runtime status unavailable."}
        # 这里只做轻量估算，不调用 tokenizer。目的是给用户一个“上下文是否在膨胀”
        # 的运行时观察窗口，而不是精确计费。
        approximate_tokens = sum(len(str(message.payload)) for message in messages)
        return {
            "mode": "local",
            "content": "\n".join(
                [
                    f"model: {tool_context.model}",
                    f"permission_mode: {tool_context.permission_mode}",
                    f"messages: {len(messages)}",
                    f"approximate_prompt_chars: {approximate_tokens}",
                    f"tools: {', '.join(tool_context.tools) if tool_context.tools else 'none'}",
                    f"always_allowed_tools: {', '.join(tool_context.app_state.get('always_allowed_tools', [])) or 'none'}",
                    f"denied_tools: {', '.join(tool_context.app_state.get('denied_tools', [])) or 'none'}",
                    f"always_ask_tools: {', '.join(tool_context.app_state.get('always_ask_tools', [])) or 'none'}",
                    f"permission_settings_sources: {', '.join(tool_context.app_state.get('permission_settings_sources', [])) or 'none'}",
                    f"read_file_state: {len(tool_context.read_file_state)}",
                    f"prompt_cache_calls: {tool_context.prompt_cache_state.call_count}",
                    f"prompt_cache_read_tokens: {tool_context.prompt_cache_state.cache_read_input_tokens}",
                    f"prompt_cache_creation_tokens: {tool_context.prompt_cache_state.cache_creation_input_tokens}",
                    f"plan_mode: {bool(tool_context.app_state.get('plan_mode', False))}",
                    f"plan_file_path: {tool_context.app_state.get('plan_file_path', 'none')}",
                    f"pending_plan_approval: {_pending_plan_approval_status(tool_context.app_state)}",
                    f"decision_gate: {tool_context.app_state.get('decision_gate', 'auto')}",
                    f"decision_gate_pending: {_decision_gate_pending_status(tool_context.app_state)}",
                    f"session_memory_path: {tool_context.session_memory_path or 'none'}",
                    f"durable_memory_dir: {tool_context.durable_memory_dir or 'none'}",
                    f"transcript_path: {tool_context.app_state.get('transcript_path', 'unknown')}",
                ]
            ),
        }

    async def _handle_tools(self, args: str, context: dict[str, object]) -> dict[str, object]:
        """内部处理该方法负责的业务逻辑。"""
        tool_context = context["tool_context"]
        if not isinstance(tool_context, ToolUseContext):
            return {"mode": "local", "content": "Tool context unavailable."}
        if not tool_context.tools:
            return {"mode": "local", "content": "No tools are currently allowed."}
        schemas = tool_context.app_state.get("tool_schemas") or []
        descriptions: dict[str, str] = {}
        if isinstance(schemas, list):
            for schema in schemas:
                if not isinstance(schema, dict):
                    continue
                function = schema.get("function")
                if isinstance(function, dict):
                    descriptions[str(function.get("name", ""))] = str(function.get("description", ""))
        return {
            "mode": "local",
            "content": "\n".join(
                f"- {tool}: {descriptions.get(tool, '')}".rstrip()
                for tool in tool_context.tools
            ),
        }

    async def _handle_skills(self, args: str, context: dict[str, object]) -> dict[str, object]:
        """显示当前加载的 skills；这是本地查询命令，不触发模型执行。"""

        tool_context = context["tool_context"]
        if not isinstance(tool_context, ToolUseContext):
            return {"mode": "local", "content": "Skill context unavailable."}
        registry = tool_context.app_state.get("skill_registry")
        if not isinstance(registry, SkillRegistry):
            registry = load_skill_registry(str(tool_context.app_state.get("cwd") or "."))
            tool_context.app_state["skill_registry"] = registry
        return {
            "mode": "local",
            "content": _format_skills_list(registry, tool_context),
        }

    async def _handle_mcp(self, args: str, context: dict[str, object]) -> dict[str, object]:
        """显示和管理 MCP server，交互菜单和脚本化子命令共享这个入口。"""

        tool_context = context["tool_context"]
        if not isinstance(tool_context, ToolUseContext):
            return {"mode": "local", "content": "MCP context unavailable."}
        servers = _mcp_servers(tool_context)
        statuses = _mcp_statuses(tool_context)
        parts = args.split()
        if not parts:
            return {"mode": "local", "content": _format_mcp_server_list(servers, statuses)}

        server_name = parts[0]
        action = parts[1] if len(parts) > 1 else "detail"
        if server_name not in servers:
            return {"mode": "local", "content": f"MCP server not found: {server_name}"}
        if action == "detail":
            return {
                "mode": "local",
                "content": _format_mcp_server_detail(server_name, servers[server_name], statuses.get(server_name, {})),
            }
        if action == "tools":
            return {
                "mode": "local",
                "content": _format_mcp_server_tools(server_name, statuses.get(server_name, {})),
            }
        if action == "reconnect":
            return {"mode": "local", "content": await _reconnect_mcp_server(tool_context, server_name)}
        if action == "disable":
            return {"mode": "local", "content": _disable_mcp_server(tool_context, server_name)}
        if action == "enable":
            return {"mode": "local", "content": await _enable_mcp_server(tool_context, server_name)}
        return {
            "mode": "local",
            "content": (
                f"Unknown MCP action: {action}\n"
                f"Available actions: /mcp {server_name} tools, /mcp {server_name} reconnect, "
                f"/mcp {server_name} disable"
            ),
        }

    async def _handle_memory_index(self, args: str, context: dict[str, object]) -> dict[str, object]:
        """内部处理该方法负责的业务逻辑。"""
        tool_context = context["tool_context"]
        if not isinstance(tool_context, ToolUseContext) or not tool_context.durable_memory_dir:
            return {"mode": "local", "content": "No durable memory directory configured."}
        index = DurableMemoryStore(tool_context.durable_memory_dir).read_index()
        return {"mode": "local", "content": index.strip() or "Memory index is empty."}

    async def _handle_plan_mode(self, args: str, context: dict[str, object]) -> dict[str, object]:
        """内部处理该方法负责的业务逻辑。"""
        tool_context = context["tool_context"]
        if not isinstance(tool_context, ToolUseContext):
            return {"mode": "local", "content": "Tool context unavailable."}
        current_mode = str(tool_context.permission_mode or tool_context.app_state.get("permission_mode") or "default")
        plan_store = PlanStore.from_app_state(tool_context.app_state)
        plan_path = plan_store.ensure()
        if current_mode != "plan":
            tool_context.app_state["pre_plan_mode"] = current_mode
        elif "pre_plan_mode" not in tool_context.app_state:
            tool_context.app_state["pre_plan_mode"] = "default"
        tool_context.permission_mode = "plan"
        tool_context.app_state["permission_mode"] = "plan"
        tool_context.app_state["plan_mode"] = True
        tool_context.app_state["plan_file_path"] = str(plan_path)
        tool_context.app_state["approved_plan"] = None
        tool_context.app_state["last_plan_mode_attachment_turn"] = 0
        if args.strip():
            return {
                "mode": "prompt",
                "content": (
                    "Plan mode is now active. Research the request and do not implement until the plan is approved. "
                    "When your final plan is ready, ask the user whether to save it to the plan file. "
                    f"If they want it saved, write only this plan file: {plan_path}.\n\n"
                    f"User request: {args.strip()}"
                ),
            }
        return {
            "mode": "local",
            "content": (
                "Plan mode enabled.\n"
                f"Plan file: {plan_path}\n"
                "When the final plan is ready, the model should ask whether to save it to this file. "
                "Use /auto to approve and exit plan mode."
            ),
        }

    async def _handle_plan_show(self, args: str, context: dict[str, object]) -> dict[str, object]:
        """内部处理该方法负责的业务逻辑。"""
        tool_context = context["tool_context"]
        if not isinstance(tool_context, ToolUseContext):
            return {"mode": "local", "content": "Tool context unavailable."}
        plan_store = PlanStore.from_app_state(tool_context.app_state)
        content = plan_store.read().strip()
        if not content:
            return {"mode": "local", "content": f"No plan yet. Plan file: {plan_store.path}"}
        return {"mode": "local", "content": f"Plan file: {plan_store.path}\n\n{content}"}

    async def _handle_auto_mode(self, args: str, context: dict[str, object]) -> dict[str, object]:
        """内部处理该方法负责的业务逻辑。"""
        tool_context = context["tool_context"]
        if not isinstance(tool_context, ToolUseContext):
            return {"mode": "local", "content": "Tool context unavailable."}
        exit_result = _exit_plan_mode(tool_context)
        saved_suffix = "" if exit_result["plan_saved"] else " No plan file was saved."
        return {
            "mode": "local",
            "content": f"Plan approved. Restored permission mode: {exit_result['restore_mode']}.{saved_suffix}",
        }

    async def _handle_compact(self, args: str, context: dict[str, object]) -> dict[str, object]:
        """内部处理该方法负责的业务逻辑。"""
        return {
            "mode": "compact",
            "content": "Conversation compaction requested.",
        }

    async def _handle_memory(self, args: str, context: dict[str, object]) -> dict[str, object]:
        """内部处理该方法负责的业务逻辑。"""
        return {
            "mode": "prompt",
            "content": "Please refresh session memory and surface relevant durable memories for the current task.",
        }


def _mcp_servers(tool_context: ToolUseContext) -> dict[str, dict[str, object]]:
    servers = tool_context.app_state.get("mcp_servers") or {}
    if not isinstance(servers, dict):
        return {}
    return {
        str(name): dict(config)
        for name, config in servers.items()
        if isinstance(config, dict)
    }


def _format_skills_list(registry: SkillRegistry, tool_context: ToolUseContext) -> str:
    """按 Claude /skills 的思路，把 skill 作为本地清单分组展示。"""

    skills = registry.list_all()
    if not skills:
        cwd = Path(str(tool_context.app_state.get("cwd") or "."))
        return "\n".join(
            [
                "Skills",
                "No skills found.",
                "",
                "Create skills in:",
                f"- {cwd / '.morty' / 'skills' / '<skill-name>' / 'SKILL.md'}",
                "- ~/.morty/skills/<skill-name>/SKILL.md",
                "- .claude/skills/<skill-name>/SKILL.md",
            ]
        )

    grouped: dict[str, list[SkillSpec]] = {
        "Project skills": [],
        "User skills": [],
    }
    cwd = Path(str(tool_context.app_state.get("cwd") or ".")).resolve()
    for skill in skills:
        try:
            skill.source_path.resolve().relative_to(cwd)
            grouped["Project skills"].append(skill)
        except ValueError:
            grouped["User skills"].append(skill)

    lines = [
        "Skills",
        f"{len(skills)} {'skill' if len(skills) == 1 else 'skills'}",
        "",
    ]
    for title in ("Project skills", "User skills"):
        group = grouped[title]
        if not group:
            continue
        lines.append(title)
        for skill in sorted(group, key=lambda item: item.name):
            flags: list[str] = []
            if not skill.model_invocable:
                flags.append("manual only")
            if skill.context == "fork":
                flags.append("fork")
            suffix = f" [{' · '.join(flags)}]" if flags else ""
            description = skill.description.strip() or "(no description)"
            lines.append(f"- {skill.name}: {description}{suffix}")
            lines.append(f"  path: {skill.source_path}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _is_plan_approval_input(text: str, tool_context: ToolUseContext) -> bool:
    """识别用户用自然语言批准计划并要求开始实现。"""

    if not bool(tool_context.app_state.get("plan_mode", False)):
        return False
    normalized = text.strip().lower()
    if not normalized:
        return False
    strong_markers = {
        "批准",
        "同意",
        "可以",
        "ok",
        "okay",
        "yes",
        "直接实现",
        "开始实现",
        "按计划实现",
        "按这个实现",
        "照着实现",
        "go on",
        "continue",
    }
    if any(marker in normalized for marker in strong_markers):
        return True
    # “继续”在中文对话里可能只是继续说明；只有已有 plan 文件时才把它视为批准。
    if "继续" in normalized:
        return PlanStore.from_app_state(tool_context.app_state).has_plan()
    return False


def _exit_plan_mode(tool_context: ToolUseContext) -> dict[str, object]:
    """退出 plan mode 并记录被批准的计划内容。"""

    was_plan_mode = bool(tool_context.app_state.get("plan_mode", False))
    plan_saved = False
    if was_plan_mode:
        plan_store = PlanStore.from_app_state(tool_context.app_state)
        plan = plan_store.read().strip()
        pending_plan = tool_context.app_state.get("pending_plan_approval")
        if not plan and isinstance(pending_plan, dict):
            plan = str(pending_plan.get("plan") or "").strip()
        plan_saved = bool(plan)
        tool_context.app_state["approved_plan"] = plan
        tool_context.app_state["plan_file_path"] = str(plan_store.path)
        tool_context.app_state.pop("pending_plan_approval", None)
    restore_mode = str(tool_context.app_state.get("pre_plan_mode") or "default")
    tool_context.permission_mode = restore_mode
    tool_context.app_state["permission_mode"] = restore_mode
    tool_context.app_state["pre_plan_mode"] = None
    tool_context.app_state["plan_mode"] = False
    if was_plan_mode:
        tool_context.app_state["needs_plan_mode_exit_attachment"] = True
    return {
        "was_plan_mode": was_plan_mode,
        "plan_saved": plan_saved,
        "restore_mode": restore_mode,
    }


def _decision_gate_pending_status(app_state: dict[str, object]) -> str:
    pending = app_state.get("decision_gate_pending")
    if isinstance(pending, dict):
        return str(pending.get("status") or "pending")
    return "none"


def _pending_plan_approval_status(app_state: dict[str, object]) -> str:
    pending = app_state.get("pending_plan_approval")
    if isinstance(pending, dict):
        return str(pending.get("status") or "pending")
    return "none"


def _mcp_statuses(tool_context: ToolUseContext) -> dict[str, dict[str, object]]:
    statuses = tool_context.app_state.get("mcp_statuses") or {}
    if not isinstance(statuses, dict):
        return {}
    return {
        str(name): dict(status)
        for name, status in statuses.items()
        if isinstance(status, dict)
    }


def _format_mcp_server_list(
    servers: dict[str, dict[str, object]],
    statuses: dict[str, dict[str, object]],
) -> str:
    """渲染 Claude 风格的 MCP 总览列表。"""

    lines = [
        "Manage MCP servers",
        _plural(len(servers), "server"),
        "",
    ]
    if not servers:
        lines.append("No MCP servers configured.")
        return "\n".join(lines)

    grouped: dict[str, list[str]] = {"user": [], "project": [], "other": []}
    for name in sorted(servers):
        config = servers[name]
        scope = str(config.get("_scope") or "other")
        status = statuses.get(name, {})
        status_text = _status_text(status)
        tool_count = _tool_count(status)
        suffix = f" · {_plural(tool_count, 'tool')}" if tool_count else ""
        if config.get("disabled") or status.get("status") == "disabled":
            suffix = " · disabled"
        grouped.setdefault(scope, []).append(f"- {name} · {status_text}{suffix}")

    for scope, title in [
        ("user", "User MCPs"),
        ("project", "Project MCPs"),
        ("other", "Other MCPs"),
    ]:
        rows = grouped.get(scope) or []
        if not rows:
            continue
        config_path = _first_config_path(servers, scope)
        header = f"{title} ({config_path})" if config_path else title
        lines.append(header)
        lines.extend(rows)
        lines.append("")
    lines.append("Use /mcp <server> to view details.")
    return "\n".join(lines).rstrip()


def _format_mcp_server_detail(
    name: str,
    config: dict[str, object],
    status: dict[str, object],
) -> str:
    args = config.get("args") if isinstance(config.get("args"), list) else []
    capabilities = status.get("capabilities")
    if not isinstance(capabilities, list) or not capabilities:
        capabilities = ["tools"] if _tool_count(status) else []
    lines = [
        f"{_title(name)} MCP Server",
        "",
        f"Status: {_status_text(status)}",
        f"Command: {config.get('command') or ''}",
        f"Args: {' '.join(str(arg) for arg in args)}",
        f"Config location: {config.get('_config_path') or 'unknown'}",
        f"Capabilities: {' · '.join(str(item) for item in capabilities) if capabilities else 'none'}",
        f"Tools: {_plural(_tool_count(status), 'tool')}",
    ]
    if status.get("error"):
        lines.append(f"Error: {status.get('error')}")
    lines.extend(
        [
            "",
            "Actions:",
            f"- /mcp {name} tools",
            f"- /mcp {name} reconnect",
            f"- /mcp {name} disable",
        ]
    )
    if config.get("disabled") or status.get("status") == "disabled":
        lines.append(f"- /mcp {name} enable")
    return "\n".join(lines)


def _format_mcp_server_tools(name: str, status: dict[str, object]) -> str:
    tools = status.get("tools")
    if not isinstance(tools, list) or not tools:
        return f"Tools for {name}\n\nNo tools are currently registered."
    lines = [f"Tools for {name}", ""]
    for index, tool in enumerate(tools, start=1):
        if not isinstance(tool, dict):
            continue
        schema = tool.get("input_schema")
        required = []
        if isinstance(schema, dict) and isinstance(schema.get("required"), list):
            required = [str(item) for item in schema["required"]]
        lines.extend(
            [
                f"{index}. {tool.get('wrapped_name') or tool.get('name')}",
                f"   Original name: {tool.get('name') or ''}",
                f"   Description: {tool.get('description') or ''}",
                f"   Required: {', '.join(required) if required else 'none'}",
            ]
        )
    return "\n".join(lines)


async def _reconnect_mcp_server(tool_context: ToolUseContext, name: str) -> str:
    servers = _mcp_servers(tool_context)
    config = servers[name]
    if config.get("disabled"):
        return f"MCP server {name} is disabled. Use /mcp {name} enable first."
    registry = tool_context.app_state.get("tool_registry")
    if not isinstance(registry, ToolRegistry):
        return "MCP tool registry unavailable; restart Morty to reconnect."
    statuses = _mcp_statuses(tool_context)
    statuses[name] = {"status": "connecting"}
    registry.remove_matching(lambda tool: tool.name.startswith(f"mcp__{name}__"))
    mcp_registry = await create_mcp_tool_registry(
        {name: _public_mcp_config(config)},
        workspace_root=Path(str(tool_context.app_state.get("cwd") or ".")),
        statuses=statuses,
    )
    tools = [
        tool
        for tool_name in mcp_registry.list_names()
        if (tool := mcp_registry.find(tool_name)) is not None
    ]
    registry.extend(tools)
    _refresh_mcp_runtime_tools(tool_context, registry, statuses)
    status = statuses.get(name, {})
    if status.get("status") == "connected":
        return f"Reconnected MCP server {name}: {_plural(_tool_count(status), 'tool')} registered."
    return f"Failed to reconnect MCP server {name}: {status.get('error') or status.get('status')}"


def _disable_mcp_server(tool_context: ToolUseContext, name: str) -> str:
    registry = tool_context.app_state.get("tool_registry")
    workspace_root = Path(str(tool_context.app_state.get("cwd") or "."))
    config_path = set_mcp_server_disabled(name=name, disabled=True, workspace_root=workspace_root)
    if isinstance(registry, ToolRegistry):
        registry.remove_matching(lambda tool: tool.name.startswith(f"mcp__{name}__"))
        tool_context.tools = registry.list_names()
        tool_context.app_state["tool_schemas"] = registry.api_tool_schemas()
    servers = load_mcp_server_entries(workspace_root)
    statuses = _mcp_statuses(tool_context)
    statuses[name] = {"status": "disabled", "tools": []}
    tool_context.app_state["mcp_servers"] = servers
    tool_context.app_state["mcp_statuses"] = statuses
    return f"Disabled MCP server {name}. Config updated: {config_path}"


async def _enable_mcp_server(tool_context: ToolUseContext, name: str) -> str:
    workspace_root = Path(str(tool_context.app_state.get("cwd") or "."))
    config_path = set_mcp_server_disabled(name=name, disabled=False, workspace_root=workspace_root)
    tool_context.app_state["mcp_servers"] = load_mcp_server_entries(workspace_root)
    message = await _reconnect_mcp_server(tool_context, name)
    return f"Enabled MCP server {name}. Config updated: {config_path}\n{message}"


def _refresh_mcp_runtime_tools(
    tool_context: ToolUseContext,
    registry: ToolRegistry,
    statuses: dict[str, dict[str, object]],
) -> None:
    tool_context.tools = registry.list_names()
    tool_context.app_state["tool_schemas"] = registry.api_tool_schemas()
    tool_context.app_state["mcp_statuses"] = statuses


def _public_mcp_config(config: dict[str, object]) -> dict[str, object]:
    return {
        key: value
        for key, value in config.items()
        if not key.startswith("_")
    }


def _status_text(status: dict[str, object]) -> str:
    return str(status.get("status") or "pending")


def _tool_count(status: dict[str, object]) -> int:
    tools = status.get("tools")
    if isinstance(tools, list):
        return len(tools)
    if isinstance(tools, int):
        return tools
    return 0


def _plural(count: int, singular: str) -> str:
    suffix = "" if count == 1 else "s"
    return f"{count} {singular}{suffix}"


def _first_config_path(servers: dict[str, dict[str, object]], scope: str) -> str | None:
    for config in servers.values():
        if config.get("_scope") == scope and config.get("_config_path"):
            return str(config["_config_path"])
    return None


def _title(name: str) -> str:
    return " ".join(part.capitalize() for part in name.replace("-", "_").split("_") if part)
