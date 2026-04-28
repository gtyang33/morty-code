from __future__ import annotations


def merge_allowed_tools(
    base_tools: list[str],
    additional_tools: list[str] | None,
) -> list[str]:
    """命令或 skill 可以临时扩展当前轮工具权限。"""

    if not additional_tools:
        return list(base_tools)
    merged = list(base_tools)
    for tool in additional_tools:
        if tool not in merged:
            merged.append(tool)
    return merged
