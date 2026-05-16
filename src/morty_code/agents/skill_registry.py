from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


CapabilityKind = Literal["skill", "slash_command", "plugin_command"]


@dataclass
class CapabilitySpec:
    """统一描述 skill、slash prompt command 和 plugin command。

    执行仍由 input/commands.py 或插件层负责；这里专注 capability discovery，
    让主线程和 forked agent 能共享同一份可见能力清单。
    """

    name: str
    kind: CapabilityKind
    description: str
    user_invocable: bool = True
    model_invocable: bool = True
    allowed_tools: list[str] = field(default_factory=list)
    metadata: dict[str, object] = field(default_factory=dict)


class SkillRegistry:
    """轻量 capability registry。"""

    def __init__(self, capabilities: list[CapabilitySpec] | None = None) -> None:
        """初始化对象状态。"""
        self._capabilities: dict[str, CapabilitySpec] = {}
        for capability in capabilities or []:
            self.register(capability)

    def register(self, capability: CapabilitySpec) -> None:
        """注册可供后续使用的条目。"""
        self._capabilities[capability.name] = capability

    def find(self, name: str) -> CapabilitySpec | None:
        """查找匹配的注册项或数据。"""
        return self._capabilities.get(name)

    def list_user_invocable(self) -> list[CapabilitySpec]:
        """列出可用条目。"""
        return [
            capability
            for capability in self._capabilities.values()
            if capability.user_invocable
        ]

    def list_model_invocable(self) -> list[CapabilitySpec]:
        """列出可用条目。"""
        return [
            capability
            for capability in self._capabilities.values()
            if capability.model_invocable
        ]

    def render_discovery_attachment(self) -> dict[str, object]:
        """生成可作为 skill_discovery attachment payload 的稳定结构。"""

        return {
            "attachment_type": "skill_discovery",
            "capabilities": [
                {
                    "name": capability.name,
                    "kind": capability.kind,
                    "description": capability.description,
                    "user_invocable": capability.user_invocable,
                    "model_invocable": capability.model_invocable,
                    "allowed_tools": capability.allowed_tools,
                }
                for capability in sorted(
                    self._capabilities.values(),
                    key=lambda item: (item.kind, item.name),
                )
            ],
        }
