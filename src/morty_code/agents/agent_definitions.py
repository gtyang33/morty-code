from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class AgentDefinition:
    """子代理定义。

    Claude Code 的 agent definition 很丰富，这里先保留最影响 runtime 行为的字段：
    system prompt、工具白名单、只读约束和最大轮数。
    """

    agent_type: str
    when_to_use: str
    system_prompt: str
    tools: list[str] = field(default_factory=lambda: ["*"])
    disallowed_tools: list[str] = field(default_factory=list)
    model: str | None = None
    max_turns: int = 4
    readonly: bool = False
    source: str = "built-in"


class AgentRegistry:
    """按优先级管理 built-in 和项目级 agent。"""

    def __init__(self, agents: list[AgentDefinition] | None = None) -> None:
        """初始化对象状态。"""
        self._agents: dict[str, AgentDefinition] = {}
        for agent in agents or default_agent_definitions():
            self.register(agent)

    def register(self, agent: AgentDefinition) -> None:
        """注册可供后续使用的条目。"""
        self._agents[agent.agent_type] = agent

    def find(self, agent_type: str) -> AgentDefinition | None:
        """查找匹配的注册项或数据。"""
        return self._agents.get(agent_type)

    def list(self) -> list[AgentDefinition]:
        """列出可用条目。"""
        return sorted(self._agents.values(), key=lambda item: item.agent_type)

    def render_discovery_text(self) -> str:
        """渲染面向用户或模型的文本。"""
        lines = ["Available subagents:"]
        for agent in self.list():
            lines.append(f"- {agent.agent_type}: {agent.when_to_use}")
        return "\n".join(lines)


def default_agent_definitions() -> list[AgentDefinition]:
    """复刻 Claude Code 的核心 built-in agents，先覆盖本地工具能力范围。"""

    general_prompt = """You are a general-purpose coding subagent.
Complete the delegated task and return a concise report for the parent agent.
Search broadly when needed, but avoid unnecessary file creation."""
    readonly_suffix = """

READ-ONLY MODE:
- Do not create, edit, delete, move, or copy files.
- Use only read-only tools available to you.
- Report findings directly instead of writing files."""
    return [
        AgentDefinition(
            agent_type="general-purpose",
            when_to_use="General research and multi-step codebase investigation.",
            system_prompt=general_prompt,
            tools=["*"],
            disallowed_tools=["spawn_agent"],
            max_turns=4,
        ),
        AgentDefinition(
            agent_type="Explore",
            when_to_use="Fast read-only codebase exploration and search.",
            system_prompt="You are a fast codebase exploration specialist." + readonly_suffix,
            tools=["read_file", "list_dir", "glob_files", "grep_text"],
            readonly=True,
            max_turns=3,
        ),
        AgentDefinition(
            agent_type="Plan",
            when_to_use="Read-only software architecture and implementation planning.",
            system_prompt="You are a software architect. Explore the codebase and design an implementation plan." + readonly_suffix,
            tools=["read_file", "list_dir", "glob_files", "grep_text"],
            readonly=True,
            max_turns=4,
        ),
        AgentDefinition(
            agent_type="verification",
            when_to_use="Read-only verification after implementation; try to find regressions.",
            system_prompt="You are a verification specialist. Try to break the implementation and report evidence." + readonly_suffix,
            tools=["read_file", "list_dir", "glob_files", "grep_text", "bash"],
            readonly=True,
            max_turns=4,
        ),
    ]


def load_project_agents(agent_dir: str | Path = ".morty/agents") -> AgentRegistry:
    """加载 `.morty/agents/*.md`。

    为避免新增 YAML 依赖，frontmatter 只支持简单 `key: value` 和逗号分隔列表。
    """

    registry = AgentRegistry()
    root = Path(agent_dir)
    if not root.exists():
        return registry
    for path in sorted(root.glob("*.md")):
        parsed = parse_agent_markdown(path)
        if parsed is not None:
            registry.register(parsed)
    return registry


def parse_agent_markdown(path: Path) -> AgentDefinition | None:
    """解析输入文本或结构化数据。"""
    text = path.read_text(encoding="utf-8", errors="replace")
    if not text.startswith("---\n"):
        return None
    _, raw_frontmatter, body = text.split("---", 2)
    fields: dict[str, str] = {}
    for line in raw_frontmatter.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        fields[key.strip()] = value.strip().strip('"').strip("'")
    name = fields.get("name")
    description = fields.get("description")
    if not name or not description:
        return None
    tools = _parse_list(fields.get("tools")) or ["*"]
    disallowed = _parse_list(fields.get("disallowed_tools"))
    readonly = fields.get("readonly", "").lower() == "true"
    try:
        max_turns = int(fields.get("max_turns", "4"))
    except ValueError:
        max_turns = 4
    return AgentDefinition(
        agent_type=name,
        when_to_use=description,
        system_prompt=body.strip(),
        tools=tools,
        disallowed_tools=disallowed,
        model=fields.get("model") or None,
        max_turns=max(1, max_turns),
        readonly=readonly,
        source=str(path),
    )


def _parse_list(raw: str | None) -> list[str]:
    """内部解析输入文本或结构化数据。"""
    if not raw:
        return []
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    return [item.strip().strip('"').strip("'") for item in raw.split(",") if item.strip()]
