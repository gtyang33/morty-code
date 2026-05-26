from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from morty_code.attachments.attachment_manager import AttachmentManager
from morty_code.input.process_user_input import UserInputProcessor
from morty_code.prompt.prompt_builder import PromptBuilder
from morty_code.prompt.prompt_sections import PromptSectionRegistry
from morty_code.skills import SkillRegistry, load_skill_registry, register_skill_tool
from morty_code.tools.tool_registry import ToolRegistry
from morty_code.tools.tool_runner import ToolRunner
from morty_code.transcript.session_restore import SessionRestore
from morty_code.types.messages import Message
from morty_code.types.runtime_state import CacheSafeParams, ContentReplacementState, QueuedCommand, ToolUseContext


def _write_skill(root: Path, name: str = "reviewer") -> Path:
    skill_dir = root / ".morty" / "skills" / name
    skill_dir.mkdir(parents=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(
        """---
description: Review code changes
when_to_use: Use before finalizing risky code changes
allowed-tools: read_file, bash
argument-hint: FILE
---
# Reviewer

Review $ARGUMENTS carefully.
Skill dir: ${MORTY_SKILL_DIR}
""",
        encoding="utf-8",
    )
    return skill_file


def _write_skill_at(skills_root: Path, name: str, description: str) -> Path:
    skill_dir = skills_root / name
    skill_dir.mkdir(parents=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(
        f"""---
description: {description}
---
# {name}

{description}
""",
        encoding="utf-8",
    )
    return skill_file


def _context(tmp_path: Path, registry: ToolRegistry | None = None) -> ToolUseContext:
    app_state: dict[str, object] = {
        "cwd": str(tmp_path),
        "tool_registry": registry or ToolRegistry(),
    }
    return ToolUseContext(
        tools=(registry or ToolRegistry()).list_names(),
        model="test-model",
        permission_mode="default",
        app_state=app_state,
        read_file_state={},
        content_replacement_state=ContentReplacementState(),
    )


def _assistant_skill_call(skill: str, args: str = "") -> Message:
    return Message(
        uuid="assistant-1",
        timestamp=datetime.now(UTC).isoformat(),
        type="assistant",
        payload={
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu-1",
                    "name": "skill",
                    "input": {"skill": skill, "args": args},
                }
            ]
        },
    )


def test_load_skill_registry_from_morty_skill_dir(tmp_path: Path) -> None:
    skill_file = _write_skill(tmp_path)

    registry = load_skill_registry(tmp_path)

    skill = registry.find("reviewer")
    assert skill is not None
    assert skill.description == "Review code changes"
    assert skill.when_to_use == "Use before finalizing risky code changes"
    assert skill.allowed_tools == ["read_file", "bash"]
    assert skill.source_path == skill_file
    assert "reviewer: Review code changes" in registry.render_listing()


def test_load_skill_registry_from_user_morty_skill_dir(tmp_path: Path) -> None:
    home = tmp_path / "home"
    skill_file = _write_skill_at(home / ".morty" / "skills", "global-reviewer", "Global review skill")

    registry = load_skill_registry(tmp_path / "workspace", home=home)

    skill = registry.find("global-reviewer")
    assert skill is not None
    assert skill.description == "Global review skill"
    assert skill.source_path == skill_file


def test_project_skill_overrides_user_skill_with_same_name(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    _write_skill_at(home / ".morty" / "skills", "reviewer", "User review skill")
    project_skill = _write_skill_at(workspace / ".morty" / "skills", "reviewer", "Project review skill")

    registry = load_skill_registry(workspace, home=home)

    skill = registry.find("reviewer")
    assert skill is not None
    assert skill.description == "Project review skill"
    assert skill.source_path == project_skill


def test_prompt_builder_injects_skill_listing_without_full_skill_body(tmp_path: Path) -> None:
    _write_skill(tmp_path)
    registry = SkillRegistry(load_skill_registry(tmp_path).list_model_invocable())
    context = _context(tmp_path)
    context.app_state["skill_registry"] = registry
    context.tools = ["skill"]

    system_prompt, _, system_context = asyncio.run(
        PromptBuilder(PromptSectionRegistry()).build_for_context(context)
    )

    prompt = "\n".join(system_prompt)
    assert "调用 skill 工具" in prompt
    assert "available_skills" in system_context
    assert "reviewer: Review code changes" in system_context["available_skills"]
    assert "Review $ARGUMENTS carefully" not in system_context["available_skills"]


def test_skill_tool_loads_full_prompt_and_records_invoked_skill(tmp_path: Path) -> None:
    _write_skill(tmp_path)
    tool_registry = ToolRegistry()
    skill_registry = load_skill_registry(tmp_path)
    register_skill_tool(tool_registry, skill_registry=skill_registry)
    context = _context(tmp_path, tool_registry)
    cache = CacheSafeParams(system_prompt=[], user_context={}, system_context={}, messages=[])

    messages = asyncio.run(
        ToolRunner(tool_registry).run(_assistant_skill_call("reviewer", "src/app.py"), context, cache)
    )

    assert len(messages) == 2
    assert messages[0].payload["content"][0]["type"] == "tool_result"
    assert "Skill loaded: reviewer" in str(messages[0].payload["content"][0]["content"])
    assert messages[1].is_meta is True
    assert "Review src/app.py carefully." in str(messages[1].payload["content"])
    assert str(tmp_path / ".morty" / "skills" / "reviewer") in str(messages[1].payload["content"])
    assert "reviewer" in context.discovered_skill_names
    assert context.app_state["invoked_skills"]["reviewer"]["content"].count("Review src/app.py carefully.") == 1
    assert "read_file" in context.tools
    assert "bash" in context.tools


def test_compact_reinjects_invoked_skill_summary(tmp_path: Path) -> None:
    context = _context(tmp_path)
    context.app_state["invoked_skills"] = {
        "reviewer": {
            "name": "reviewer",
            "path": str(tmp_path / ".morty" / "skills" / "reviewer" / "SKILL.md"),
            "content": "full skill content",
        }
    }

    attachments = AttachmentManager().collect_reinjection(context, [])

    invoked = [attachment for attachment in attachments if attachment.type == "invoked_skills"]
    assert len(invoked) == 1
    assert invoked[0].payload["skills"][0]["name"] == "reviewer"
    assert invoked[0].payload["skills"][0]["content"] == "full skill content"


def test_session_restore_recovers_invoked_skills_attachment(tmp_path: Path) -> None:
    message = Message(
        uuid="attachment-1",
        timestamp=datetime.now(UTC).isoformat(),
        type="attachment",
        payload={
            "attachment_type": "invoked_skills",
            "skills": [
                {
                    "name": "reviewer",
                    "path": str(tmp_path / ".morty" / "skills" / "reviewer" / "SKILL.md"),
                    "content": "full skill content",
                }
            ],
        },
        is_meta=True,
    )

    restored = SessionRestore().restore([message], {"cwd": str(tmp_path)})

    invoked = restored["tool_context"].app_state["invoked_skills"]
    assert invoked["reviewer"]["content"] == "full skill content"


def test_skills_slash_command_lists_skills_grouped_by_source(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    _write_skill_at(home / ".morty" / "skills", "global-reviewer", "Global review skill")
    _write_skill_at(workspace / ".morty" / "skills", "project-reviewer", "Project review skill")
    context = _context(workspace)
    context.app_state["skill_registry"] = load_skill_registry(workspace, home=home)

    processed = asyncio.run(
        UserInputProcessor(AttachmentManager()).process(
            QueuedCommand(value="/skills", mode="prompt"),
            context,
            [],
        )
    )

    assert processed.should_query is False
    content = str(processed.messages[0].payload["content"])
    assert "Skills" in content
    assert "Project skills" in content
    assert "User skills" in content
    assert "project-reviewer" in content
    assert "Project review skill" in content
    assert "global-reviewer" in content
    assert "Global review skill" in content


def test_skills_slash_command_shows_create_hint_when_empty(tmp_path: Path) -> None:
    context = _context(tmp_path)
    context.app_state["skill_registry"] = SkillRegistry()

    processed = asyncio.run(
        UserInputProcessor(AttachmentManager()).process(
            QueuedCommand(value="/skills", mode="prompt"),
            context,
            [],
        )
    )

    assert processed.should_query is False
    content = str(processed.messages[0].payload["content"])
    assert "No skills found" in content
    assert ".morty/skills" in content
