from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from morty_code.types.messages import Message


@dataclass
class CacheSafeParams:
    """主线程与子线程共享的 cache-critical 前缀。"""

    system_prompt: list[str]
    user_context: dict[str, str]
    system_context: dict[str, str]
    messages: list[Message]


@dataclass
class FileViewState:
    """记录模型见过的文件视图，而不是普通文件缓存。"""

    path: str
    content: str
    timestamp: float | None = None
    offset: int | None = None
    limit: int | None = None
    is_partial_view: bool = False


@dataclass
class ContentReplacementRecord:
    """显式记录大结果被预算替换后的稳定文本。"""

    tool_use_id: str
    replacement: str


@dataclass
class ContentReplacementState:
    """保存大 tool result 的替换决策，确保 resume 后 prompt bytes 稳定。"""

    seen_ids: set[str] = field(default_factory=set)
    replacements: dict[str, str] = field(default_factory=dict)


@dataclass
class PromptCacheRuntimeState:
    """记录 prompt cache 的请求稳定性和 provider usage。"""

    previous_hashes: dict[str, str] = field(default_factory=dict)
    call_count: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


@dataclass
class ToolUseContext:
    """整个 query runtime 的可变状态总线。"""

    tools: list[str]
    model: str
    permission_mode: str
    app_state: dict[str, Any]
    read_file_state: dict[str, FileViewState]
    content_replacement_state: ContentReplacementState
    prompt_cache_state: PromptCacheRuntimeState = field(default_factory=PromptCacheRuntimeState)
    loaded_nested_memory_paths: set[str] = field(default_factory=set)
    discovered_skill_names: set[str] = field(default_factory=set)
    session_memory_path: str | None = None
    durable_memory_dir: str | None = None


@dataclass
class QueuedCommand:
    """统一用户输入、系统通知、后台结果回流的内部命令对象。"""

    value: str | list[dict[str, Any]]
    mode: str
    pre_expansion_value: str | None = None
    pasted_contents: dict[int, dict[str, Any]] | None = None
    skip_slash_commands: bool = False
    bridge_origin: bool = False
    is_meta: bool = False
    uuid: str | None = None
    origin: dict[str, Any] | None = None


@dataclass
class ProcessedUserInput:
    """query 前的输入处理结果。"""

    messages: list[Message]
    should_query: bool
    allowed_tools: list[str] | None = None
    model: str | None = None
    effort: str | None = None
    next_input: str | None = None
    submit_next_input: bool = False
    # slash command 可以请求本地 compact，而不是把“请压缩”再交给模型回答。
    trigger_compact: bool = False


@dataclass
class LoadedTranscript:
    """从 transcript 读取后的统一载体。"""

    messages: list[Message]
    metadata_events: list[dict[str, Any]]
    last_parent_uuid: str | None = None
