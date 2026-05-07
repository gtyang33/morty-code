from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Callable
from uuid import uuid4

from morty_code.api.errors import ModelProviderError
from morty_code.attachments.attachment_manager import AttachmentManager
from morty_code.cache.prompt_cache import (
    PromptCacheBreakDetector,
    PromptCachePlanner,
    extract_cache_usage,
)
from morty_code.agents.subagent_tool import register_subagent_tool
from morty_code.agents.task_output_tool import register_task_output_tool
from morty_code.tools.tool_result_budget import apply_tool_result_budget
from morty_code.transcript.message_normalizer import MessageNormalizer
from morty_code.types.messages import Message
from morty_code.types.runtime_state import CacheSafeParams, ToolUseContext


@dataclass
class QueryLoopResult:
    new_messages: list[Message]
    metadata_events: list[dict[str, object]]


class QueryLoop:
    """模型采样、工具执行和 post-iteration attachment 的主循环。

    当前职责：
    - normalize
    - model sampling
    - tool runner 回灌
    """

    def __init__(
        self,
        model_client,
        tool_runner,
        attachment_manager: AttachmentManager | None = None,
        max_iterations: int = 6,
        max_api_retries: int = 2,
    ) -> None:
        self.model_client = model_client
        self.tool_runner = tool_runner
        registry = getattr(tool_runner, "registry", None)
        if registry is not None:
            register_subagent_tool(self, registry)
            register_task_output_tool(registry)
        self.normalizer = MessageNormalizer()
        self.attachment_manager = attachment_manager or AttachmentManager()
        self.max_iterations = max_iterations
        self.max_api_retries = max_api_retries
        self.cache_detector = PromptCacheBreakDetector()

    async def run(
        self,
        messages: list[Message],
        cache_safe: CacheSafeParams,
        tool_context: ToolUseContext,
        max_iterations: int | None = None,
        on_new_messages: Callable[[list[Message]], None] | None = None,
    ) -> QueryLoopResult:
        new_messages: list[Message] = []
        metadata_events: list[dict[str, object]] = []
        working_messages = list(messages)
        assistant_message: Message | None = None
        iteration_limit = self.max_iterations if max_iterations is None else max(1, max_iterations)
        hit_iteration_limit_with_tools = False
        for _ in range(iteration_limit):
            # 大 tool_result 在进入下一次模型请求前做稳定替换。替换记录保存在
            # content_replacement_state，确保同一个 tool_use_id 后续恢复时仍使用
            # 同一个占位文本，而不是每轮重新决定。
            working_messages, replacement_records = apply_tool_result_budget(
                working_messages,
                tool_context.content_replacement_state,
                tool_results_dir=tool_context.app_state.get(
                    "tool_results_dir",
                    ".morty/tool-results",
                ),
                limit=int(tool_context.app_state.get("tool_result_message_budget_chars", 50000)),
                skip_tool_names=set(tool_context.app_state.get("tool_result_budget_skip_tools", [])),
            )
            if replacement_records:
                metadata_events.append(
                    {
                        "type": "content-replacement",
                        "replacements": [
                            record.as_event_payload() for record in replacement_records
                        ],
                    }
                )
            api_messages = self.normalizer.normalize_for_api(working_messages, tool_context.tools)
            normalization_event = self.normalizer.last_report.to_event()
            if normalization_event is not None:
                metadata_events.append(normalization_event)
            tool_schemas = self._load_tool_schemas(
                cache_safe.system_context.get("tool_schemas_json"),
                metadata_events,
            )
            # prompt cache 规划默认只记录在 runtime 内部；只有 send_cache_control
            # 打开时才把 cache_control 字段发给兼容网关，避免普通 OpenAI-compatible
            # 服务因为不认识扩展字段而 400。
            cache_plan = PromptCachePlanner(
                enable_prompt_caching=bool(tool_context.app_state.get("enable_prompt_caching", True)),
                use_global_scope=bool(tool_context.app_state.get("use_global_prompt_cache_scope", True)),
                cache_ttl=str(tool_context.app_state.get("prompt_cache_ttl") or "") or None,
            ).prepare(
                messages=api_messages,
                system_prompt=cache_safe.system_prompt,
                tool_schemas=tool_schemas,
                skip_cache_write=bool(tool_context.app_state.get("skip_cache_write", False)),
            )
            cache_event = self.cache_detector.record(
                tool_context.prompt_cache_state,
                system_blocks=cache_plan["system_blocks"],
                tool_schemas=cache_plan["tool_schemas"],
                model=tool_context.model,
                messages=cache_plan["messages"],
            )
            if cache_event is not None:
                metadata_events.append(cache_event)
            request_system_context = dict(cache_safe.system_context)
            request_system_context["prompt_cache_plan_json"] = self._json_dumps(cache_plan)
            request_messages = api_messages
            if tool_context.app_state.get("send_cache_control"):
                request_messages = cache_plan["messages"]
                request_system_context["tool_schemas_json"] = self._json_dumps(cache_plan["tool_schemas"])
            # 每次 respond 返回一个 assistant message。它可能是普通文本，也可能
            # 是一组 tool_use；工具结果会被回灌成 user/tool_result 后进入下一轮。
            assistant_message = await self._respond_with_retries(
                messages=request_messages,
                fallback_messages=api_messages,
                system_prompt=cache_safe.system_prompt,
                user_context=cache_safe.user_context,
                system_context=request_system_context,
                fallback_system_context=cache_safe.system_context,
                metadata_events=metadata_events,
            )
            if assistant_message.payload.get("is_api_error"):
                new_messages.append(assistant_message)
                self._emit_new_messages(on_new_messages, [assistant_message])
                break
            usage = extract_cache_usage(assistant_message.payload)
            if usage["cache_read_input_tokens"] or usage["cache_creation_input_tokens"]:
                tool_context.prompt_cache_state.cache_read_input_tokens += usage["cache_read_input_tokens"]
                tool_context.prompt_cache_state.cache_creation_input_tokens += usage["cache_creation_input_tokens"]
                metadata_events.append(
                    {
                        "type": "prompt-cache-usage",
                        **usage,
                        "total_cache_read_input_tokens": tool_context.prompt_cache_state.cache_read_input_tokens,
                        "total_cache_creation_input_tokens": tool_context.prompt_cache_state.cache_creation_input_tokens,
                    }
                )
            new_messages.append(assistant_message)
            working_messages.append(assistant_message)
            self._emit_new_messages(on_new_messages, [assistant_message])

            tool_messages = await self.tool_runner.run(assistant_message, tool_context, cache_safe)
            # ToolRunner 把权限、启动、成功/失败等事件临时塞到 app_state。
            # QueryLoop 在这里 drain 成 metadata event，避免污染 prompt 状态。
            tool_events = tool_context.app_state.pop("tool_execution_events", [])
            if isinstance(tool_events, list):
                metadata_events.extend(event for event in tool_events if isinstance(event, dict))
            if not tool_messages:
                break
            new_messages.extend(tool_messages)
            working_messages.extend(tool_messages)
            self._emit_new_messages(on_new_messages, tool_messages)
        else:
            # for-else 只有在没有 break 的情况下触发：说明达到了工具迭代上限。
            # 如果最后一条 assistant 仍包含 tool_use，需要再做一次无工具总结，
            # 否则用户会只看到最后一批 tool_result，没有结论。
            hit_iteration_limit_with_tools = bool(
                assistant_message is not None
                and self._message_has_tool_uses(assistant_message)
            )
        if assistant_message is None:
            return QueryLoopResult(new_messages=[], metadata_events=metadata_events)
        if hit_iteration_limit_with_tools:
            final_messages = await self._finalize_after_iteration_limit(
                working_messages=working_messages,
                cache_safe=cache_safe,
                tool_context=tool_context,
                metadata_events=metadata_events,
                iteration_limit=iteration_limit,
            )
            new_messages.extend(final_messages)
            working_messages.extend(final_messages)
            self._emit_new_messages(on_new_messages, final_messages)
        # post-iteration attachment 用于 date_change、plan 状态、memory re-inject
        # 等“本轮结束后才知道”的上下文，进入 transcript 但通常不会立刻展示。
        post_attachments = await self.attachment_manager.collect_post_iteration(
            input_text="",
            context=tool_context,
            messages=working_messages,
            queued_commands=[],
        )
        attachment_messages = [
            self.attachment_manager.to_message(
                attachment,
                timestamp=assistant_message.timestamp,
            )
            for attachment in post_attachments
        ]
        self._emit_new_messages(on_new_messages, attachment_messages)
        return QueryLoopResult(
            new_messages=[*new_messages, *attachment_messages],
            metadata_events=metadata_events,
        )

    async def _finalize_after_iteration_limit(
        self,
        *,
        working_messages: list[Message],
        cache_safe: CacheSafeParams,
        tool_context: ToolUseContext,
        metadata_events: list[dict[str, object]],
        iteration_limit: int,
    ) -> list[Message]:
        """工具迭代耗尽后追加一次无工具总结，避免 CLI 停在 tool_result。"""

        # 这条 meta user message 不代表真实用户输入，只是给模型一个明确边界：
        # 不允许继续调用工具，必须基于已获得事实收束回答。
        metadata_events.append(
            {
                "type": "tool-iteration-limit-finalize",
                "max_iterations": iteration_limit,
            }
        )
        final_instruction = Message(
            uuid=str(uuid4()),
            timestamp=datetime.now(UTC).isoformat(),
            type="user",
            payload={
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "工具调用轮次已达到上限。请只基于已经获得的工具结果给出"
                            "最终结论、关键依据和下一步建议，不要再调用工具。"
                        ),
                    }
                ]
            },
            is_meta=True,
        )
        final_api_messages = self.normalizer.normalize_for_api(
            [*working_messages, final_instruction],
            [],
        )
        final_system_context = dict(cache_safe.system_context)
        final_system_context.pop("tool_schemas_json", None)
        assistant_message = await self._respond_with_retries(
            messages=final_api_messages,
            fallback_messages=final_api_messages,
            system_prompt=cache_safe.system_prompt,
            user_context=cache_safe.user_context,
            system_context=final_system_context,
            fallback_system_context=final_system_context,
            metadata_events=metadata_events,
        )
        return [final_instruction, assistant_message]

    @staticmethod
    def _message_has_tool_uses(message: Message) -> bool:
        content = message.payload.get("content")
        if not isinstance(content, list):
            return False
        return any(
            isinstance(block, dict) and block.get("type") == "tool_use"
            for block in content
        )

    @staticmethod
    def _emit_new_messages(
        callback: Callable[[list[Message]], None] | None,
        messages: list[Message],
    ) -> None:
        if callback is not None and messages:
            callback(messages)

    async def _respond_with_retries(
        self,
        messages: list[dict[str, object]],
        fallback_messages: list[dict[str, object]],
        system_prompt: list[str],
        user_context: dict[str, str],
        system_context: dict[str, str],
        fallback_system_context: dict[str, str],
        metadata_events: list[dict[str, object]],
    ) -> Message:
        cache_disabled_for_retry = False
        active_messages = messages
        active_system_context = system_context
        attempt = 1
        while attempt <= self.max_api_retries + 1:
            try:
                return await self.model_client.respond(
                    messages=active_messages,
                    system_prompt=system_prompt,
                    user_context=user_context,
                    system_context=active_system_context,
                )
            except ModelProviderError as exc:
                if exc.status == 400 and not cache_disabled_for_retry and active_messages is not fallback_messages:
                    cache_disabled_for_retry = True
                    active_messages = fallback_messages
                    active_system_context = fallback_system_context
                    metadata_events.append(
                        {
                            "type": "prompt-cache-disabled-for-retry",
                            "status": exc.status,
                            "detail": _shorten(exc.detail or str(exc)),
                        }
                    )
                    # cache 字段兼容性降级不消耗 API retry 预算。
                    continue
                if attempt <= self.max_api_retries and exc.retryable:
                    delay = self._retry_delay(attempt, exc.retry_after)
                    metadata_events.append(
                        {
                            "type": "api-retry",
                            "attempt": attempt,
                            "max_retries": self.max_api_retries,
                            "delay_seconds": delay,
                            "status": exc.status,
                            "error": _shorten(exc.detail or str(exc)),
                        }
                    )
                    await asyncio.sleep(delay)
                    attempt += 1
                    continue
                metadata_events.append(
                    {
                        "type": "query_failed",
                        "status": exc.status,
                        "retryable": exc.retryable,
                        "error": _shorten(exc.detail or str(exc), 2000),
                    }
                )
                return self._api_error_message(exc.detail or str(exc), status=exc.status)
            except Exception as exc:  # noqa: BLE001 - 未分类 provider bug 也要转成 transcript 消息。
                metadata_events.append(
                    {
                        "type": "query_failed",
                        "status": None,
                        "retryable": False,
                        "error": _shorten(str(exc), 2000),
                    }
                )
                return self._api_error_message(str(exc), status=None)
        return self._api_error_message("model provider retry loop exhausted", status=None)

    def _load_tool_schemas(
        self,
        tool_schemas_json: str | None,
        metadata_events: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        if not tool_schemas_json:
            return []
        try:
            import json

            loaded = json.loads(tool_schemas_json)
            return loaded if isinstance(loaded, list) else []
        except Exception as exc:  # noqa: BLE001 - cache plan 失败应降级，不应中断 turn。
            metadata_events.append(
                {
                    "type": "prompt-cache-plan-failed",
                    "reason": "tool_schemas_json_parse_failed",
                    "error": _shorten(str(exc)),
                }
            )
            return []

    def _api_error_message(self, content: str, status: int | None) -> Message:
        prefix = f"Model provider error"
        if status is not None:
            prefix += f" HTTP {status}"
        return Message(
            uuid=str(uuid4()),
            timestamp=datetime.now(UTC).isoformat(),
            type="assistant",
            payload={
                "content": [{"type": "text", "text": f"{prefix}: {_shorten(content, 4000)}"}],
                "is_api_error": True,
                "status": status,
            },
            is_meta=True,
        )

    def _retry_delay(self, attempt: int, retry_after: float | None) -> float:
        if retry_after is not None:
            return min(retry_after, 5.0)
        return min(0.25 * (2 ** (attempt - 1)), 2.0)

    def _json_dumps(self, value: object) -> str:
        import json

        return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _shorten(value: str, limit: int = 500) -> str:
    value = " ".join(value.split())
    if len(value) <= limit:
        return value
    return f"{value[: limit - 3]}..."
