from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from morty_code.types.messages import Message
from morty_code.types.runtime_state import ContentReplacementState


PERSISTED_OUTPUT_TAG = "<persisted-output>"
PERSISTED_OUTPUT_CLOSING_TAG = "</persisted-output>"
PREVIEW_SIZE_CHARS = 2000
DEFAULT_RESULT_BUDGET_CHARS = 50000
DEFAULT_MESSAGE_BUDGET_CHARS = 200000


@dataclass
class ToolResultCandidate:
    tool_use_id: str
    content: object
    size: int


@dataclass
class ToolResultReplacementRecord:
    kind: str
    tool_use_id: str
    replacement: str

    def as_event_payload(self) -> dict[str, object]:
        """转换为目标表示。"""
        return {
            "kind": self.kind,
            "tool_use_id": self.tool_use_id,
            "replacement": self.replacement,
        }


def apply_tool_result_budget(
    messages: list[Message],
    state: ContentReplacementState,
    tool_results_dir: str | Path,
    limit: int = DEFAULT_MESSAGE_BUDGET_CHARS,
    skip_tool_names: set[str] | None = None,
) -> tuple[list[Message], list[ToolResultReplacementRecord]]:
    """按 API user message group 对 tool_result 做 aggregate budget。

    这里复刻 Claude Code 的关键语义：同一个 tool_use_id 第一次见到时决定
    是否替换，后续 turn 只重放相同决策，避免 prompt cache prefix 漂移。
    """

    skip_tool_names = skip_tool_names or set()
    tool_name_by_id = _build_tool_name_map(messages)
    replacement_map: dict[str, str] = {}
    newly_replaced: list[ToolResultReplacementRecord] = []
    to_persist: list[ToolResultCandidate] = []

    for group in _collect_candidates_by_wire_message(messages):
        # budget 按“最终会发给 API 的 user message group”计算，而不是按单个
        # tool_result 计算；这样能控制一次 tool 回灌整体不会超过 provider 限制。
        must_reapply, frozen, fresh = _partition_by_prior_decision(group, state)
        for candidate, replacement in must_reapply:
            replacement_map[candidate.tool_use_id] = replacement

        if not fresh:
            for candidate in group:
                state.seen_ids.add(candidate.tool_use_id)
            continue

        skipped = [
            candidate
            for candidate in fresh
            if tool_name_by_id.get(candidate.tool_use_id, "") in skip_tool_names
        ]
        for candidate in skipped:
            state.seen_ids.add(candidate.tool_use_id)
        eligible = [
            candidate
            for candidate in fresh
            if tool_name_by_id.get(candidate.tool_use_id, "") not in skip_tool_names
        ]

        frozen_size = sum(candidate.size for candidate in frozen)
        # frozen 是历史上已决定保留完整文本的结果，不能为了本轮预算突然改写；
        # 只有 fresh 候选可以被选中持久化到 .morty/tool-results。
        selected = (
            _select_largest_until_under_budget(eligible, frozen_size, limit)
            if frozen_size + sum(candidate.size for candidate in eligible) > limit
            else []
        )
        selected_ids = {candidate.tool_use_id for candidate in selected}
        for candidate in group:
            if candidate.tool_use_id not in selected_ids:
                state.seen_ids.add(candidate.tool_use_id)
        to_persist.extend(selected)

    if to_persist:
        Path(tool_results_dir).mkdir(parents=True, exist_ok=True)
    for candidate in to_persist:
        # 完整内容落盘，prompt 中只保留稳定占位符。用户需要时仍可从
        # .morty/tool-results/<tool_use_id>.txt 查看原始结果。
        replacement = _persist_and_build_replacement(candidate, tool_results_dir)
        state.seen_ids.add(candidate.tool_use_id)
        state.replacements[candidate.tool_use_id] = replacement
        replacement_map[candidate.tool_use_id] = replacement
        newly_replaced.append(
            ToolResultReplacementRecord(
                kind="tool-result",
                tool_use_id=candidate.tool_use_id,
                replacement=replacement,
            )
        )

    if not replacement_map:
        return messages, []
    return _replace_tool_result_contents(messages, replacement_map), newly_replaced


def _build_tool_name_map(messages: list[Message]) -> dict[str, str]:
    """内部构建后续流程需要的数据。"""
    mapping: dict[str, str] = {}
    for message in messages:
        if message.type != "assistant":
            continue
        content = message.payload.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                mapping[str(block.get("id", ""))] = str(block.get("name", ""))
    return mapping


def _collect_candidates_by_wire_message(messages: list[Message]) -> list[list[ToolResultCandidate]]:
    """内部收集当前阶段需要的上下文。"""
    groups: list[list[ToolResultCandidate]] = []
    current: list[ToolResultCandidate] = []
    seen_assistant_ids: set[str] = set()

    def flush() -> None:
        """处理该方法负责的业务逻辑。"""
        nonlocal current
        if current:
            groups.append(current)
            current = []

    for message in messages:
        if message.type == "user":
            current.extend(_collect_candidates_from_message(message))
        elif message.type == "assistant":
            assistant_id = message.uuid
            if assistant_id not in seen_assistant_ids:
                flush()
                seen_assistant_ids.add(assistant_id)
    flush()
    return groups


def _collect_candidates_from_message(message: Message) -> list[ToolResultCandidate]:
    """内部收集当前阶段需要的上下文。"""
    content = message.payload.get("content")
    if not isinstance(content, list):
        return []
    candidates: list[ToolResultCandidate] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        result_content = block.get("content")
        if _is_empty(result_content) or _has_image_block(result_content):
            continue
        if isinstance(result_content, str) and result_content.startswith(PERSISTED_OUTPUT_TAG):
            continue
        candidates.append(
            ToolResultCandidate(
                tool_use_id=str(block.get("tool_use_id", "")),
                content=result_content,
                size=_content_size(result_content),
            )
        )
    return [candidate for candidate in candidates if candidate.tool_use_id]


def _partition_by_prior_decision(
    candidates: list[ToolResultCandidate],
    state: ContentReplacementState,
) -> tuple[list[tuple[ToolResultCandidate, str]], list[ToolResultCandidate], list[ToolResultCandidate]]:
    """内部处理该方法负责的业务逻辑。"""
    must_reapply: list[tuple[ToolResultCandidate, str]] = []
    frozen: list[ToolResultCandidate] = []
    fresh: list[ToolResultCandidate] = []
    for candidate in candidates:
        replacement = state.replacements.get(candidate.tool_use_id)
        if replacement is not None:
            must_reapply.append((candidate, replacement))
        elif candidate.tool_use_id in state.seen_ids:
            frozen.append(candidate)
        else:
            fresh.append(candidate)
    return must_reapply, frozen, fresh


def _select_largest_until_under_budget(
    fresh: list[ToolResultCandidate],
    frozen_size: int,
    limit: int,
) -> list[ToolResultCandidate]:
    """内部处理该方法负责的业务逻辑。"""
    selected: list[ToolResultCandidate] = []
    remaining = frozen_size + sum(candidate.size for candidate in fresh)
    for candidate in sorted(fresh, key=lambda item: item.size, reverse=True):
        if remaining <= limit:
            break
        selected.append(candidate)
        remaining -= candidate.size
    return selected


def _persist_and_build_replacement(
    candidate: ToolResultCandidate,
    tool_results_dir: str | Path,
) -> str:
    """内部处理该方法负责的业务逻辑。"""
    content = _stringify_content(candidate.content)
    path = Path(tool_results_dir) / f"{candidate.tool_use_id}.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(content, encoding="utf-8")
    preview = _preview(content)
    suffix = "\n...\n" if len(preview) < len(content) else "\n"
    return (
        f"{PERSISTED_OUTPUT_TAG}\n"
        f"Output too large ({len(content)} chars). Full output saved to: {path}\n\n"
        f"Preview (first {PREVIEW_SIZE_CHARS} chars):\n"
        f"{preview}{suffix}"
        f"{PERSISTED_OUTPUT_CLOSING_TAG}"
    )


def _replace_tool_result_contents(
    messages: list[Message],
    replacement_map: dict[str, str],
) -> list[Message]:
    """内部处理该方法负责的业务逻辑。"""
    replaced_messages: list[Message] = []
    for message in messages:
        content = message.payload.get("content")
        if message.type != "user" or not isinstance(content, list):
            replaced_messages.append(message)
            continue
        changed = False
        new_content: list[object] = []
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_result"
                and str(block.get("tool_use_id", "")) in replacement_map
            ):
                updated = dict(block)
                updated["content"] = replacement_map[str(block.get("tool_use_id", ""))]
                new_content.append(updated)
                changed = True
            else:
                new_content.append(block)
        if not changed:
            replaced_messages.append(message)
            continue
        replaced_messages.append(
            Message(
                uuid=message.uuid,
                timestamp=message.timestamp,
                type=message.type,
                payload={**message.payload, "content": new_content},
                is_meta=message.is_meta,
                is_virtual=message.is_virtual,
                origin=message.origin,
            )
        )
    return replaced_messages


def _is_empty(content: object) -> bool:
    """内部判断当前对象是否满足条件。"""
    if content is None:
        return True
    if isinstance(content, str):
        return not content.strip()
    if isinstance(content, list):
        return not content
    return False


def _has_image_block(content: object) -> bool:
    """内部判断当前对象是否包含目标内容。"""
    return isinstance(content, list) and any(
        isinstance(block, dict) and block.get("type") == "image"
        for block in content
    )


def _content_size(content: object) -> int:
    """内部处理该方法负责的业务逻辑。"""
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(
            len(str(block.get("text", "")))
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return len(_stringify_content(content))


def _stringify_content(content: object) -> str:
    """内部处理该方法负责的业务逻辑。"""
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False, indent=2)


def _preview(content: str) -> str:
    """内部处理该方法负责的业务逻辑。"""
    if len(content) <= PREVIEW_SIZE_CHARS:
        return content
    truncated = content[:PREVIEW_SIZE_CHARS]
    last_newline = truncated.rfind("\n")
    cut = last_newline if last_newline > PREVIEW_SIZE_CHARS * 0.5 else PREVIEW_SIZE_CHARS
    return content[:cut]
