from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ToolPermissionRule:
    """解析后的工具权限规则。

    `Bash(npm:*)` 这类内容规则只匹配 Bash 的 command，不会把整个 Bash 工具放开。
    """

    tool_name: str
    content: str | None = None


def parse_tool_rule(rule: str) -> ToolPermissionRule:
    """解析输入文本或结构化数据。"""
    text = rule.strip()
    open_index = _first_unescaped(text, "(")
    close_index = _last_unescaped(text, ")")
    if open_index <= 0 or close_index != len(text) - 1 or close_index <= open_index:
        return ToolPermissionRule(tool_name=text)
    tool_name = text[:open_index].strip()
    raw_content = text[open_index + 1 : close_index]
    if not tool_name:
        return ToolPermissionRule(tool_name=text)
    if raw_content in {"", "*"}:
        return ToolPermissionRule(tool_name=tool_name)
    return ToolPermissionRule(tool_name=tool_name, content=_unescape_rule_content(raw_content))


def rule_matches_tool(rule: str, tool_name: str) -> bool:
    """处理该方法负责的业务逻辑。"""
    parsed = parse_tool_rule(rule)
    return parsed.tool_name.lower() == tool_name.lower() and parsed.content is None


def rule_matches_bash_command(rule: str, command: str) -> bool:
    """处理该方法负责的业务逻辑。"""
    parsed = parse_tool_rule(rule)
    if parsed.tool_name.lower() != "bash" or parsed.content is None:
        return False
    return shell_pattern_matches(parsed.content, command)


def shell_pattern_matches(pattern: str, command: str) -> bool:
    """处理该方法负责的业务逻辑。"""
    normalized_pattern = pattern.strip()
    normalized_command = command.strip()
    if normalized_pattern.endswith(":*"):
        prefix = normalized_pattern[:-2]
        return normalized_command == prefix or normalized_command.startswith(prefix + " ")
    if _has_unescaped_wildcard(normalized_pattern):
        return _wildcard_match(normalized_pattern, normalized_command)
    return normalized_command == _unescape_shell_literal(normalized_pattern)


def _wildcard_match(pattern: str, command: str) -> bool:
    """内部处理该方法负责的业务逻辑。"""
    parts: list[str] = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "\\" and index + 1 < len(pattern):
            parts.append(re.escape(pattern[index + 1]))
            index += 2
            continue
        if char == "*":
            parts.append(".*")
        else:
            parts.append(re.escape(char))
        index += 1
    regex = "".join(parts)
    if regex.endswith(r"\ .*") and pattern.count("*") == 1:
        regex = regex[: -len(r"\ .*")] + r"( .*)?"
    return re.fullmatch(regex, command, flags=re.DOTALL) is not None


def _has_unescaped_wildcard(pattern: str) -> bool:
    """内部判断当前对象是否包含目标内容。"""
    if pattern.endswith(":*"):
        return False
    for index, char in enumerate(pattern):
        if char != "*":
            continue
        backslashes = 0
        cursor = index - 1
        while cursor >= 0 and pattern[cursor] == "\\":
            backslashes += 1
            cursor -= 1
        if backslashes % 2 == 0:
            return True
    return False


def _first_unescaped(text: str, char: str) -> int:
    """内部处理该方法负责的业务逻辑。"""
    for index, current in enumerate(text):
        if current == char and not _is_escaped(text, index):
            return index
    return -1


def _last_unescaped(text: str, char: str) -> int:
    """内部处理该方法负责的业务逻辑。"""
    for index in range(len(text) - 1, -1, -1):
        if text[index] == char and not _is_escaped(text, index):
            return index
    return -1


def _is_escaped(text: str, index: int) -> bool:
    """内部判断当前对象是否满足条件。"""
    backslashes = 0
    cursor = index - 1
    while cursor >= 0 and text[cursor] == "\\":
        backslashes += 1
        cursor -= 1
    return backslashes % 2 == 1


def _unescape_rule_content(content: str) -> str:
    """内部处理该方法负责的业务逻辑。"""
    return content.replace(r"\(", "(").replace(r"\)", ")").replace(r"\\", "\\")


def _unescape_shell_literal(pattern: str) -> str:
    """内部处理该方法负责的业务逻辑。"""
    return pattern.replace(r"\*", "*").replace(r"\\", "\\")
