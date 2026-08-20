"""已发现 MCP 工具的目录，以及它们到内部能力的映射。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from models.contracts import AgentName, ToolDescriptor
from .policy import AGENT_CAPABILITIES


CAPABILITY_HINTS: dict[str, tuple[str, ...]] = {
    "web_search": ("search_web", "web_search", "internet_search", "brave_search", "search"),
    "fetch_page": ("fetch_page", "fetch_url", "read_url", "get_page", "scrape", "fetch"),
    "place_search": ("place_search", "search_places", "maps_search", "geocode", "place"),
    "route_search": ("route_search", "directions", "route", "distance_matrix"),
    "weather_forecast": ("weather_forecast", "forecast", "weather"),
    "market_data": ("market_data", "ticker_info", "quote", "stock_price", "finance"),
}

# 该项目仅处理文本决策，禁止多模态、上下文拼装和耗时的深度研究工具暴露给模型。
BLOCKED_REMOTE_TOOL_NAMES = frozenset({
    "brave_image_search", "brave_video_search", "brave_llm_context", "tavily_research",
})


@dataclass(frozen=True)
class RegisteredTool:
    descriptor: ToolDescriptor
    provider: str
    remote_name: str
    session: Any


class ToolRegistry:
    def __init__(self) -> None:
        self._by_capability: dict[str, list[RegisteredTool]] = {}

    def register(self, provider: str, remote: Any, session: Any) -> RegisteredTool | None:
        data = remote if isinstance(remote, dict) else {
            "name": getattr(remote, "name", ""),
            "description": getattr(remote, "description", ""),
            "inputSchema": getattr(remote, "inputSchema", getattr(remote, "input_schema", {})),
        }
        name = str(data.get("name", ""))
        if name.casefold().replace("-", "_") in BLOCKED_REMOTE_TOOL_NAMES:
            return None
        capability = self.map_capability(name, str(data.get("description", "")))
        if capability is None:
            return None
        allowed_agents = [AgentName(agent) for agent, capabilities in AGENT_CAPABILITIES.items() if capability in capabilities]
        descriptor = ToolDescriptor(
            name=name, capability=capability,
            description=str(data.get("description") or name),
            input_schema=data.get("inputSchema") or data.get("input_schema") or {},
            allowed_agents=allowed_agents, read_only=True,
        )
        registered = RegisteredTool(descriptor, provider, name, session)
        self._by_capability.setdefault(capability, []).append(registered)
        return registered

    @staticmethod
    def map_capability(name: str, description: str = "") -> str | None:
        haystack = f"{name} {description}".casefold().replace("-", "_")
        ranked = sorted(
            ((len(hint), capability) for capability, hints in CAPABILITY_HINTS.items() for hint in hints if hint in haystack),
            reverse=True,
        )
        return ranked[0][1] if ranked else None

    def providers(self, capability: str) -> list[RegisteredTool]:
        return list(self._by_capability.get(capability, []))

    def get(self, remote_name: str) -> RegisteredTool | None:
        """按模型明确选择的远程工具名返回唯一工具，拒绝含糊的同名提供方。"""
        matches = [
            item for providers in self._by_capability.values() for item in providers
            if item.remote_name == remote_name
        ]
        return matches[0] if len(matches) == 1 else None

    def list_capabilities(self) -> list[ToolDescriptor]:
        return [item.descriptor for capability in sorted(self._by_capability) for item in self._by_capability[capability]]

    def clear_provider(self, provider: str) -> None:
        for capability in list(self._by_capability):
            self._by_capability[capability] = [item for item in self._by_capability[capability] if item.provider != provider]
            if not self._by_capability[capability]:
                del self._by_capability[capability]
