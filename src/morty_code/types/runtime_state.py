from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from morty_code.types.messages import Message


_FORK_SHARED_APP_STATE_KEYS = {
    "tool_registry",
}


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


def clone_tool_use_context_for_fork(
    context: ToolUseContext,
    *,
    fork_label: str = "forked_agent",
    skip_cache_write: bool = False,
) -> ToolUseContext:
    """为 forked agent 显式 clone 可变状态。

    不直接暴露 `deepcopy(context)`，是为了把 Claude Code 里的关键协议写清楚：
    read_file_state / content replacement 要 clone，避免 fork 改父线程；
    prompt cache 的 cache-safe 参数由调用方继承；runtime detector 统计从零开始，
    避免 fork 多出来的 prompt 被误报为父线程 cache break。
    """

    app_state = _clone_app_state_for_fork(context.app_state)
    app_state["fork"] = {
        "label": fork_label,
        "isolated": True,
        "skip_cache_write": skip_cache_write,
        "parent_prompt_cache_hashes": dict(context.prompt_cache_state.previous_hashes),
    }
    if skip_cache_write:
        app_state["skip_cache_write"] = True

    return ToolUseContext(
        tools=list(context.tools),
        model=context.model,
        permission_mode=context.permission_mode,
        app_state=app_state,
        read_file_state=deepcopy(context.read_file_state),
        content_replacement_state=ContentReplacementState(
            seen_ids=set(context.content_replacement_state.seen_ids),
            replacements=dict(context.content_replacement_state.replacements),
        ),
        prompt_cache_state=PromptCacheRuntimeState(),
        loaded_nested_memory_paths=set(context.loaded_nested_memory_paths),
        discovered_skill_names=set(context.discovered_skill_names),
        session_memory_path=context.session_memory_path,
        durable_memory_dir=context.durable_memory_dir,
    )


def _clone_app_state_for_fork(app_state: dict[str, Any]) -> dict[str, Any]:
    """克隆 fork 可变状态，同时保留运行时服务对象引用。"""

    cloned: dict[str, Any] = {}
    for key, value in app_state.items():
        if key in _FORK_SHARED_APP_STATE_KEYS:
            # tool_registry 内含 RLock，不能 deepcopy；它是线程安全的共享服务，
            # fork 子代理只需要查 schema/工具定义，保留同一引用即可。
            cloned[key] = value
            continue
        cloned[key] = deepcopy(value)
    return cloned


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
