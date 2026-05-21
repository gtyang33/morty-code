from __future__ import annotations

import json
import re


def format_tool_result_summary(content: object, *, max_chars: int = 400) -> str:
    """把工具结果压成适合人读的一行摘要。"""
    parsed = parse_json_like_content(content)
    if isinstance(parsed, dict):
        if _looks_like_mcp_result(parsed):
            return _format_mcp_result(parsed, max_chars=max_chars)
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


def _looks_like_mcp_result(content: dict[str, object]) -> bool:
    blocks = content.get("content")
    return isinstance(blocks, list) and any(
        isinstance(block, dict) and block.get("type") == "text"
        for block in blocks
    )


def _format_mcp_result(content: dict[str, object], *, max_chars: int) -> str:
    """格式化 MCP tool 返回的 content blocks，避免把 JSON-RPC 外壳直接刷屏。"""

    blocks = content.get("content")
    if not isinstance(blocks, list):
        return truncate_text(json.dumps(content, ensure_ascii=False, sort_keys=True), max_chars)
    texts = [
        str(block.get("text") or "")
        for block in blocks
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    execution_time = _extract_execution_time(texts)
    data_text = next((text for text in texts if _starts_like_json(text)), "")
    parsed_data = parse_json_like_content(data_text) if data_text else None
    if isinstance(parsed_data, list):
        summary = _format_json_rows(parsed_data)
    elif isinstance(parsed_data, dict):
        summary = _format_json_object(parsed_data)
    else:
        summary = one_line("\n".join(texts), max_chars)
    if execution_time:
        summary = f"{summary} · {execution_time}" if summary else execution_time
    if content.get("isError"):
        summary = f"error: {summary}" if summary else "error"
    return truncate_text(summary, max_chars)


def _starts_like_json(text: str) -> bool:
    stripped = text.lstrip()
    return stripped.startswith("[") or stripped.startswith("{")


def _extract_execution_time(texts: list[str]) -> str:
    for text in texts:
        match = re.search(r"Query execution time:\s*([0-9.]+\s*ms)", text)
        if match:
            return re.sub(r"\s+", " ", match.group(1)).strip()
    return ""


def _format_json_rows(rows: list[object]) -> str:
    row_count = len(rows)
    row_word = "row" if row_count == 1 else "rows"
    dict_rows = [row for row in rows if isinstance(row, dict)]
    if not dict_rows:
        return f"{row_count} {row_word}"
    first = dict_rows[0]
    columns = [str(key) for key in first.keys()]
    if row_count == 1 and len(first) <= 3:
        values = ", ".join(f"{key}={_compact_scalar(value)}" for key, value in first.items())
        return f"1 row · {values}"
    preview_rows = []
    for row in dict_rows[:3]:
        preview_rows.append(
            ", ".join(
                f"{key}={_compact_scalar(value)}"
                for key, value in list(row.items())[:3]
            )
        )
    suffix = "" if len(dict_rows) <= 3 else f"; ... +{len(dict_rows) - 3}"
    return (
        f"{row_count} {row_word} · columns: {', '.join(columns[:8])}"
        f" · preview: {'; '.join(preview_rows)}{suffix}"
    )


def _format_json_object(value: dict[str, object]) -> str:
    values = ", ".join(
        f"{key}={_compact_scalar(item)}"
        for key, item in list(value.items())[:6]
    )
    suffix = "" if len(value) <= 6 else f", ... +{len(value) - 6}"
    return f"object · {values}{suffix}"


def _compact_scalar(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, (int, float, bool)):
        return str(value)
    return truncate_text(str(value), 60)


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
