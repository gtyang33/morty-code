from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from morty_code.types.runtime_state import ToolUseContext


DEFAULT_SKILL_CHAR_BUDGET = 8000
MAX_LISTING_DESC_CHARS = 250


@dataclass(frozen=True)
class SkillSpec:
    """模型可调用 skill 的统一描述。

    这里只保存轻量 metadata 和按需展开函数需要的信息；完整 SKILL.md
    内容只在 skill tool 被调用时注入，避免常驻 prompt 膨胀。
    """

    name: str
    description: str
    content: str
    source_path: Path
    base_dir: Path
    when_to_use: str | None = None
    allowed_tools: list[str] = field(default_factory=list)
    argument_hint: str | None = None
    model: str | None = None
    effort: str | None = None
    context: str | None = None
    agent: str | None = None
    user_invocable: bool = True
    model_invocable: bool = True

    def render_prompt(self, args: str, context: ToolUseContext) -> str:
        """按需展开完整 skill prompt，并替换 Claude 兼容变量。"""

        rendered = f"Base directory for this skill: {self.base_dir}\n\n{self.content}"
        rendered = rendered.replace("$ARGUMENTS", args)
        rendered = rendered.replace("${MORTY_SKILL_DIR}", str(self.base_dir))
        rendered = rendered.replace("${CLAUDE_SKILL_DIR}", str(self.base_dir))
        rendered = rendered.replace(
            "${MORTY_SESSION_ID}",
            str(context.app_state.get("session_id") or ""),
        )
        rendered = rendered.replace(
            "${CLAUDE_SESSION_ID}",
            str(context.app_state.get("session_id") or ""),
        )
        return rendered


class SkillRegistry:
    """Claude 风格 skill registry：轻量发现，按需加载全文。"""

    def __init__(self, skills: list[SkillSpec] | None = None) -> None:
        self._skills: dict[str, SkillSpec] = {}
        for skill in skills or []:
            self.register(skill)

    def register(self, skill: SkillSpec) -> None:
        self._skills[skill.name] = skill

    def find(self, name: str) -> SkillSpec | None:
        normalized = name.strip().removeprefix("/")
        return self._skills.get(normalized)

    def list_all(self) -> list[SkillSpec]:
        """列出全部 skill，供 /skills 这类用户可见命令展示。"""

        return sorted(self._skills.values(), key=lambda item: item.name)

    def list_model_invocable(self) -> list[SkillSpec]:
        return [
            skill
            for skill in self.list_all()
            if skill.model_invocable
        ]

    def render_listing(self, *, char_budget: int = DEFAULT_SKILL_CHAR_BUDGET) -> str:
        """渲染给模型看的轻量 skill 列表，永远不包含完整正文。"""

        lines: list[str] = []
        used = 0
        for skill in self.list_model_invocable():
            description = skill.description
            if skill.when_to_use:
                description = f"{description} - {skill.when_to_use}"
            if len(description) > MAX_LISTING_DESC_CHARS:
                description = description[: MAX_LISTING_DESC_CHARS - 1] + "…"
            line = f"- {skill.name}: {description}"
            if lines and used + len(line) + 1 > char_budget:
                break
            lines.append(line)
            used += len(line) + 1
        return "\n".join(lines)


def load_skill_registry(cwd: str | Path, *, home: str | Path | None = None) -> SkillRegistry:
    """从 Morty/Claude 兼容目录加载 skill。

    支持用户级 `~/.morty/skills`，以及项目级 `.morty/skills` 与
    `.claude/skills`。加载顺序从全局到项目，保证项目内同名 skill 覆盖全局默认。
    """

    root = Path(cwd)
    home_root = Path(home).expanduser() if home is not None else Path.home()
    registry = SkillRegistry()
    for skills_dir in (
        home_root / ".morty" / "skills",
        root / ".morty" / "skills",
        root / ".claude" / "skills",
    ):
        for skill in _load_skills_from_dir(skills_dir):
            registry.register(skill)
    return registry


def _load_skills_from_dir(skills_dir: Path) -> list[SkillSpec]:
    if not skills_dir.exists() or not skills_dir.is_dir():
        return []
    skills: list[SkillSpec] = []
    for entry in sorted(skills_dir.iterdir(), key=lambda item: item.name):
        if not entry.is_dir():
            continue
        skill_file = entry / "SKILL.md"
        if not skill_file.exists():
            continue
        raw = skill_file.read_text(encoding="utf-8", errors="replace")
        frontmatter, content = _split_frontmatter(raw)
        skills.append(
            SkillSpec(
                name=entry.name,
                description=str(frontmatter.get("description") or _first_heading(content) or entry.name),
                when_to_use=_optional_string(frontmatter.get("when_to_use")),
                allowed_tools=_parse_list(frontmatter.get("allowed-tools")),
                argument_hint=_optional_string(frontmatter.get("argument-hint")),
                model=_optional_string(frontmatter.get("model")),
                effort=_optional_string(frontmatter.get("effort")),
                context=_optional_string(frontmatter.get("context")),
                agent=_optional_string(frontmatter.get("agent")),
                user_invocable=_parse_bool(frontmatter.get("user-invocable"), default=True),
                model_invocable=not _parse_bool(frontmatter.get("disable-model-invocation"), default=False),
                content=content.strip(),
                source_path=skill_file,
                base_dir=entry,
            )
        )
    return skills


def _split_frontmatter(raw: str) -> tuple[dict[str, object], str]:
    if not raw.startswith("---"):
        return {}, raw
    lines = raw.splitlines()
    end_index = None
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end_index = index
            break
    if end_index is None:
        return {}, raw
    frontmatter = _parse_frontmatter_lines(lines[1:end_index])
    content = "\n".join(lines[end_index + 1 :])
    return frontmatter, content


def _parse_frontmatter_lines(lines: list[str]) -> dict[str, object]:
    result: dict[str, object] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        result[key.strip()] = value.strip().strip("\"'")
    return result


def _parse_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [item.strip() for item in str(value).split(",") if item.strip()]


def _parse_bool(value: object, *, default: bool) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _first_heading(content: str) -> str | None:
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
    return None
