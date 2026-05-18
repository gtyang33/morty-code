from __future__ import annotations

import json


def format_tool_result_summary(content: object, *, max_chars: int = 400) -> str:
    """把工具结果压成适合人读的一行摘要。"""
    parsed = parse_json_like_content(content)
    if isinstance(parsed, dict):
        if "command" in parsed:
            return _format_command_result(parsed, max_chars=max_chars)
        if "path" in parsed:
            return _format_path_result(parsed)
        if "filenames" in parsed:
            return _format_filenames_result(parsed)
        if "output" in parsed:
            return f"output: {one_line(str(parsed.get('output') or ''), min(max_chars, 320))}"
        if "status" in parsed:
            return f"status={parsed.get('status')}"
        return truncate_text(json.dumps(parsed, ensure_ascii=False, sort_keys=True), max_chars)
    if isinstance(parsed, list):
        return truncate_text(_format_structured_blocks(parsed), max_chars)
    if isinstance(parsed, str):
        if parsed.lstrip().startswith("<persisted-output>"):
            return "[persisted tool result]"
        if parsed.startswith("[Tool result ") and "was replaced" in parsed:
            return "large result hidden; full content is kept in transcript/tool-results"
        return truncate_text(parsed, max_chars)
    return truncate_text(json.dumps(parsed, ensure_ascii=False, sort_keys=True), max_chars)


def parse_json_like_content(content: object) -> object:
    """解析可能以 JSON 字符串保存的工具结果。"""
    if not isinstance(content, str):
        return content
    stripped = content.strip()
    if not stripped or stripped[0] not in "{[":
        return content
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return content


def one_line(text: str, limit: int) -> str:
    """把多行文本压成一行，保留相邻行之间的分隔感。"""
    return truncate_text(" / ".join(part.strip() for part in text.splitlines() if part.strip()), limit)


def truncate_text(text: str, limit: int) -> str:
    """压缩空白并按字符数裁剪。"""
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: max(0, limit - 3)] + "..."


def _format_command_result(content: dict[str, object], *, max_chars: int) -> str:
    """格式化 bash/命令工具结果。"""
    command = one_line(str(content.get("command") or ""), min(max_chars, 180))
    exit_code = content.get("exit_code")
    timed_out = content.get("timed_out")
    parts = [f"command=`{command}`"]
    if exit_code is not None:
        parts.append(f"exit={exit_code}")
    if timed_out:
        parts.append("timed_out")
    stdout = one_line(str(content.get("stdout") or ""), min(max_chars, 220))
    stderr = one_line(str(content.get("stderr") or ""), min(max_chars, 220))
    if stdout:
        parts.append(f"stdout: {stdout}")
    if stderr:
        parts.append(f"stderr: {stderr}")
    return " ".join(parts)


def _format_path_result(content: dict[str, object]) -> str:
    """格式化文件/目录工具结果，避免把文件正文重新塞进摘要。"""
    path = str(content.get("path") or "")
    if "entries" in content:
        entries = content.get("entries")
        count = len(entries) if isinstance(entries, list) else "unknown"
        suffix = " truncated" if content.get("truncated") else ""
        return f"list_dir {path}: {count} entries{suffix}"
    if "line_count" in content:
        suffix = " truncated" if content.get("truncated") else ""
        return f"file={path} lines={content.get('line_count')}{suffix}"
    if "size" in content:
        return f"path={path} size={content.get('size')}"
    return f"path={path}"


def _format_filenames_result(content: dict[str, object]) -> str:
    """格式化 grep/glob 一类文件列表结果。"""
    filenames = content.get("filenames")
    if not isinstance(filenames, list):
        return "filenames=<unknown>"
    preview = ", ".join(str(item) for item in filenames[:5])
    suffix = "" if len(filenames) <= 5 else f", ... +{len(filenames) - 5}"
    return f"grep matched {len(filenames)} files: {preview}{suffix}".rstrip()


def _format_structured_blocks(content: list[object]) -> str:
    """格式化多模态/结构化 block，避免媒体原文进入摘要。"""
    parts: list[str] = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            parts.append(str(item.get("text", "")))
        elif isinstance(item, dict) and item.get("type") == "image":
            parts.append("[image]")
        elif isinstance(item, dict) and item.get("type") == "document":
            parts.append("[document]")
        elif isinstance(item, dict) and item.get("type") == "tool_reference":
            parts.append("[tool reference removed]")
        elif isinstance(item, dict):
            parts.append("[structured tool content]")
        else:
            parts.append(str(item))
    return "\n".join(part for part in parts if part)
