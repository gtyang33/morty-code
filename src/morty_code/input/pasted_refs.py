from __future__ import annotations

import re


REFERENCE_RE = re.compile(
    r"\[(Pasted text|Image|\.\.\.Truncated text) #(\d+)(?: \+\d+ lines)?(\.)*\]"
)


def parse_references(input_text: str) -> list[dict[str, int | str]]:
    """解析 pasted text/image 引用占位符。"""

    refs: list[dict[str, int | str]] = []
    for match in REFERENCE_RE.finditer(input_text):
        ref_id = int(match.group(2))
        if ref_id <= 0:
            continue
        refs.append(
            {
                "id": ref_id,
                "match": match.group(0),
                "index": match.start(),
            }
        )
    return refs


def expand_pasted_text_refs(
    input_text: str,
    pasted_contents: dict[int, dict[str, object]],
) -> str:
    """只展开文本 paste，图片引用保留为结构化资源。"""

    refs = parse_references(input_text)
    expanded = input_text
    for ref in reversed(refs):
        content = pasted_contents.get(int(ref["id"]))
        if not content or content.get("type") != "text":
            continue
        value = str(content.get("content", ""))
        start = int(ref["index"])
        match = str(ref["match"])
        expanded = expanded[:start] + value + expanded[start + len(match) :]
    return expanded
