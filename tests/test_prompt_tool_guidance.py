from __future__ import annotations

import asyncio

from morty_code.prompt.prompt_builder import PromptBuilder
from morty_code.prompt.prompt_sections import PromptSectionRegistry
from morty_code.tools.builtin_tools import create_local_tool_registry
from morty_code.tools.tool_registry import ToolRegistry, ToolSpec


def test_system_prompt_prefers_structured_file_edit_tools(tmp_path) -> None:
    builder = PromptBuilder(PromptSectionRegistry())

    system_prompt, _, _ = asyncio.run(
        builder.build(
            ["read_file", "edit_file", "multi_edit", "write_file", "bash"],
            "test-model",
            {"cwd": str(tmp_path)},
        )
    )
    prompt = "\n".join(system_prompt)

    assert "edit_file" in prompt
    assert "multi_edit" in prompt
    assert "write_file" in prompt
    assert "不要用 bash" in prompt
    assert "sed -i" in prompt
    assert "python3 -c" in prompt


def test_tool_descriptions_steer_models_away_from_shell_file_edits(tmp_path) -> None:
    registry = create_local_tool_registry(tmp_path)
    schemas = registry.api_tool_schemas()
    descriptions = {
        str(schema["function"]["name"]): str(schema["function"]["description"])
        for schema in schemas
    }

    assert "Use bash for:" in descriptions["bash"]
    assert "Run tests and builds" in descriptions["bash"]
    assert "Use dedicated tools instead of bash:" in descriptions["bash"]
    assert "File search: use glob_files" in descriptions["bash"]
    assert "Content search: use grep_text" in descriptions["bash"]
    assert "Read files: use read_file" in descriptions["bash"]
    assert "Edit existing files: use edit_file or multi_edit" in descriptions["bash"]
    assert "Write new files or full rewrites: use write_file" in descriptions["bash"]
    assert "Do not use sed -i" in descriptions["bash"]
    assert "Do not create Python scripts just to rewrite files" in descriptions["bash"]
    assert "Prefer edit_file" in descriptions["write_file"]
    assert "read_file first" in descriptions["edit_file"]
    assert "old_string must be unique" in descriptions["edit_file"]
    assert "several exact replacements" in descriptions["multi_edit"]


def test_tool_schema_uses_model_prompt_without_changing_short_description() -> None:
    async def handler(args: dict[str, object]) -> dict[str, object]:
        return args

    registry = ToolRegistry(
        [
            ToolSpec(
                name="demo",
                description="Short UI description.",
                prompt="Long model-facing prompt with usage rules.",
                handler=handler,
            )
        ]
    )

    schemas = registry.api_tool_schemas()

    assert registry.find("demo").description == "Short UI description."
    assert schemas[0]["function"]["description"] == "Long model-facing prompt with usage rules."
