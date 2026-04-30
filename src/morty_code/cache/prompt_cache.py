from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from morty_code.prompt.prompt_builder import SYSTEM_PROMPT_DYNAMIC_BOUNDARY


CacheScope = str


@dataclass
class SystemPromptBlock:
    """发送到 API 的 system prompt 块及其 cache scope。"""

    text: str
    cache_scope: CacheScope | None = None


class PromptCachePlanner:
    """生成服务端 prompt cache 所需的请求形状。

    Claude Code 的 cache 模块重点不是本地复用模型回答，而是稳定服务端
    prompt cache key：system prompt、tools、messages prefix、model 都要尽量
    保持相同字节。这里保留这条主线，默认只生成计划；是否透传到 provider
    由调用方决定。
    """

    def __init__(
        self,
        enable_prompt_caching: bool = True,
        use_global_scope: bool = True,
        cache_ttl: str | None = None,
    ) -> None:
        self.enable_prompt_caching = enable_prompt_caching
        self.use_global_scope = use_global_scope
        self.cache_ttl = cache_ttl

    def prepare(
        self,
        messages: list[dict[str, object]],
        system_prompt: list[str],
        tool_schemas: list[dict[str, object]],
        skip_cache_write: bool = False,
    ) -> dict[str, object]:
        cache_control = get_cache_control(ttl=self.cache_ttl)
        system_blocks = build_system_prompt_blocks(
            system_prompt,
            enable_prompt_caching=self.enable_prompt_caching,
            use_global_scope=self.use_global_scope,
            cache_control=cache_control,
        )
        cached_messages = add_message_cache_breakpoint(
            messages,
            enable_prompt_caching=self.enable_prompt_caching,
            cache_control=cache_control,
            skip_cache_write=skip_cache_write,
        )
        cached_tools = annotate_tool_schemas(
            tool_schemas,
            enable_prompt_caching=self.enable_prompt_caching,
            cache_control=cache_control,
        )
        return {
            "system_blocks": system_blocks,
            "messages": cached_messages,
            "tool_schemas": cached_tools,
            "enabled": self.enable_prompt_caching,
            "skip_cache_write": skip_cache_write,
        }


class PromptCacheBreakDetector:
    """记录 cache-critical 输入是否漂移。

    这不是失败判定，而是把“为什么 cache miss”变成 transcript metadata，
    后续 resume/fork 时能看到是 system、tools、model 还是 cache_control 变了。
    """

    def record(
        self,
        state: Any,
        *,
        system_blocks: list[dict[str, object]],
        tool_schemas: list[dict[str, object]],
        model: str,
        messages: list[dict[str, object]],
    ) -> dict[str, object] | None:
        state.call_count += 1
        current = {
            "system": _stable_hash(_strip_cache_control(system_blocks)),
            "tools": _stable_hash(_strip_cache_control(tool_schemas)),
            "cache_control": _stable_hash(_collect_cache_controls(system_blocks, tool_schemas, messages)),
            "model": model,
            "message_prefix": _stable_hash(_message_prefix(messages)),
        }
        if not state.previous_hashes:
            state.previous_hashes = current
            return {
                "type": "prompt-cache-state",
                "call_count": state.call_count,
                "status": "initialized",
                "hashes": current,
            }

        changed = sorted(
            key for key, value in current.items() if state.previous_hashes.get(key) != value
        )
        previous = state.previous_hashes
        state.previous_hashes = current
        if not changed:
            return None
        return {
            "type": "prompt-cache-break",
            "call_count": state.call_count,
            "changed": changed,
            "previous_hashes": {key: previous.get(key) for key in changed},
            "new_hashes": {key: current.get(key) for key in changed},
        }


def get_cache_control(ttl: str | None = None, scope: str | None = None) -> dict[str, str]:
    control = {"type": "ephemeral"}
    if ttl == "1h":
        control["ttl"] = "1h"
    if scope == "global":
        control["scope"] = "global"
    return control


def split_system_prompt_prefix(
    system_prompt: list[str],
    use_global_scope: bool = True,
) -> list[SystemPromptBlock]:
    """按 Claude Code 的 dynamic boundary 切分 cacheable system prompt。"""

    if use_global_scope and SYSTEM_PROMPT_DYNAMIC_BOUNDARY in system_prompt:
        boundary_index = system_prompt.index(SYSTEM_PROMPT_DYNAMIC_BOUNDARY)
        static_blocks: list[str] = []
        dynamic_blocks: list[str] = []
        for index, block in enumerate(system_prompt):
            if not block or block == SYSTEM_PROMPT_DYNAMIC_BOUNDARY:
                continue
            if index < boundary_index:
                static_blocks.append(block)
            else:
                dynamic_blocks.append(block)
        result: list[SystemPromptBlock] = []
        if static_blocks:
            result.append(SystemPromptBlock("\n\n".join(static_blocks), "global"))
        if dynamic_blocks:
            result.append(SystemPromptBlock("\n\n".join(dynamic_blocks), None))
        return result
    joined = "\n\n".join(block for block in system_prompt if block and block != SYSTEM_PROMPT_DYNAMIC_BOUNDARY)
    return [SystemPromptBlock(joined, "org")] if joined else []


def build_system_prompt_blocks(
    system_prompt: list[str],
    enable_prompt_caching: bool,
    use_global_scope: bool = True,
    cache_control: dict[str, str] | None = None,
) -> list[dict[str, object]]:
    blocks: list[dict[str, object]] = []
    for block in split_system_prompt_prefix(system_prompt, use_global_scope=use_global_scope):
        rendered: dict[str, object] = {"type": "text", "text": block.text}
        if enable_prompt_caching and block.cache_scope is not None:
            control = dict(cache_control or get_cache_control())
            if block.cache_scope == "global":
                control["scope"] = "global"
            rendered["cache_control"] = control
        blocks.append(rendered)
    return blocks


def annotate_tool_schemas(
    tool_schemas: list[dict[str, object]],
    enable_prompt_caching: bool,
    cache_control: dict[str, str] | None = None,
) -> list[dict[str, object]]:
    """只在最后一个 tool schema 上加 cache marker，避免超过 API marker 限制。"""

    copied = deepcopy(tool_schemas)
    if enable_prompt_caching and copied:
        copied[-1]["cache_control"] = dict(cache_control or get_cache_control())
    return copied


def add_message_cache_breakpoint(
    messages: list[dict[str, object]],
    enable_prompt_caching: bool,
    cache_control: dict[str, str] | None = None,
    skip_cache_write: bool = False,
) -> list[dict[str, object]]:
    """给最后一个可缓存消息块加 cache_control，并给旧 tool_result 加 cache_reference。"""

    copied = deepcopy(messages)
    if not enable_prompt_caching or not copied:
        return copied
    marker_index = len(copied) - 2 if skip_cache_write and len(copied) > 1 else len(copied) - 1
    marker_index = max(0, marker_index)
    _add_cache_control_to_message(copied[marker_index], cache_control or get_cache_control())
    for message in copied[:marker_index]:
        _add_cache_references_to_tool_results(message)
    return copied


def extract_cache_usage(payload: dict[str, object]) -> dict[str, int]:
    """兼容 Anthropic 与 OpenAI-style usage 字段。"""

    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return {"cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    cache_read = int(usage.get("cache_read_input_tokens") or 0)
    cache_create = int(usage.get("cache_creation_input_tokens") or 0)
    details = usage.get("prompt_tokens_details")
    if isinstance(details, dict):
        cache_read += int(details.get("cached_tokens") or 0)
    return {
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_create,
    }


def _add_cache_control_to_message(message: dict[str, object], cache_control: dict[str, str]) -> None:
    content = message.get("content")
    if isinstance(content, str):
        message["content"] = [{"type": "text", "text": content, "cache_control": dict(cache_control)}]
        return
    if not isinstance(content, list):
        return
    for index in range(len(content) - 1, -1, -1):
        block = content[index]
        if not isinstance(block, dict):
            continue
        if block.get("type") in {"thinking", "redacted_thinking"}:
            continue
        updated = dict(block)
        updated["cache_control"] = dict(cache_control)
        content[index] = updated
        return


def _add_cache_references_to_tool_results(message: dict[str, object]) -> None:
    if message.get("role") != "user":
        return
    content = message.get("content")
    if not isinstance(content, list):
        return
    for index, block in enumerate(content):
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        tool_use_id = block.get("tool_use_id")
        if tool_use_id:
            updated = dict(block)
            updated["cache_reference"] = str(tool_use_id)
            content[index] = updated


def _strip_cache_control(items: list[dict[str, object]]) -> list[dict[str, object]]:
    stripped: list[dict[str, object]] = []
    for item in items:
        copied = dict(item)
        copied.pop("cache_control", None)
        stripped.append(copied)
    return stripped


def _collect_cache_controls(*groups: list[dict[str, object]]) -> list[object]:
    controls: list[object] = []
    for group in groups:
        for item in group:
            if "cache_control" in item:
                controls.append(item["cache_control"])
            content = item.get("content")
            if isinstance(content, list):
                controls.extend(
                    block.get("cache_control")
                    for block in content
                    if isinstance(block, dict) and "cache_control" in block
                )
    return controls


def _message_prefix(messages: list[dict[str, object]]) -> list[dict[str, object]]:
    if len(messages) <= 1:
        return messages
    return messages[:-1]


def _stable_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]
