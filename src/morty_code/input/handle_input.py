from __future__ import annotations

from morty_code.input.pasted_refs import expand_pasted_text_refs, parse_references
from morty_code.types.runtime_state import QueuedCommand


class InputDispatcher:
    """最外层输入归一化器。

    负责把原始输入统一变成 QueuedCommand。
    """

    async def submit(
        self,
        raw_input: str,
        mode: str,
        pasted_contents: dict[int, dict[str, object]] | None = None,
    ) -> list[QueuedCommand]:
        return self.submit_sync(raw_input, mode, pasted_contents)

    def submit_sync(
        self,
        raw_input: str,
        mode: str,
        pasted_contents: dict[int, dict[str, object]] | None = None,
    ) -> list[QueuedCommand]:
        pasted_contents = pasted_contents or {}
        referenced_ids = {int(ref["id"]) for ref in parse_references(raw_input)}
        filtered = {
            key: value
            for key, value in pasted_contents.items()
            if value.get("type") != "image" or int(value.get("id", key)) in referenced_ids
        }
        final_input = expand_pasted_text_refs(raw_input, filtered)
        return [
            QueuedCommand(
                value=final_input,
                pre_expansion_value=raw_input,
                mode=mode,
                pasted_contents=filtered or None,
            )
        ]
