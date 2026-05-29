from __future__ import annotations

from pathlib import Path

from morty_code.input.image_input import convert_inline_images
from morty_code.input.pasted_refs import expand_pasted_text_refs, parse_references
from morty_code.types.runtime_state import QueuedCommand


class InputDispatcher:
    """最外层输入归一化器。

    负责把原始输入统一变成 QueuedCommand。
    """

    def __init__(self, workspace_root: str | Path | None = None) -> None:
        """初始化输入归一化器。"""
        self.workspace_root = Path(workspace_root).expanduser().resolve() if workspace_root is not None else Path.cwd()

    async def submit(
        self,
        raw_input: str,
        mode: str,
        pasted_contents: dict[int, dict[str, object]] | None = None,
    ) -> list[QueuedCommand]:
        """提交用户输入并驱动一次处理。"""
        return self.submit_sync(raw_input, mode, pasted_contents)

    def submit_sync(
        self,
        raw_input: str,
        mode: str,
        pasted_contents: dict[int, dict[str, object]] | None = None,
    ) -> list[QueuedCommand]:
        """提交用户输入并驱动一次处理。"""
        pasted_contents = pasted_contents or {}
        if not pasted_contents:
            converted = convert_inline_images(raw_input, cwd=self.workspace_root)
            if converted is not None:
                raw_input = converted.text
                pasted_contents = converted.pasted_contents
        referenced_ids = {int(ref["id"]) for ref in parse_references(raw_input)}
        filtered = {
            key: value
            for key, value in pasted_contents.items()
            if value.get("type") != "image" or int(value.get("id", key)) in referenced_ids
        }
        raw_input = self._mark_missing_image_refs(raw_input, referenced_ids, filtered)
        final_input = expand_pasted_text_refs(raw_input, filtered)
        return [
            QueuedCommand(
                value=final_input,
                pre_expansion_value=raw_input,
                mode=mode,
                pasted_contents=filtered or None,
            )
        ]

    def _mark_missing_image_refs(
        self,
        raw_input: str,
        referenced_ids: set[int],
        pasted_contents: dict[int, dict[str, object]],
    ) -> str:
        """标记没有实际 payload 的图片引用，避免模型误读裸占位符。"""

        missing = [
            image_id
            for image_id in sorted(referenced_ids)
            if image_id not in pasted_contents
        ]
        updated = raw_input
        for image_id in missing:
            updated = updated.replace(
                f"[Image #{image_id}]",
                f"[Missing image #{image_id}: paste or attach the image again]",
            )
        return updated
