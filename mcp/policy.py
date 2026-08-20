"""对外部决策研究工具执行默认拒绝的授权策略。"""

from __future__ import annotations

import re
from typing import Any

from models.contracts import AgentName


FORBIDDEN_TERMS = frozenset({
    "execute", "exec", "shell", "command", "install", "delete", "remove",
    "write", "upload", "send_money", "transfer", "place_order", "order",
    "buy", "sell", "trade", "book", "reserve", "purchase", "submit",
    "accept_offer", "accept", "签约", "下单", "购买", "预订", "删除", "写入",
    "转账", "交易", "接受offer", "接受 offer",
})

READ_ONLY_VERBS = frozenset({
    "search", "fetch", "query", "read", "list", "view", "compare", "get",
    "find", "lookup", "forecast", "搜索", "获取", "查询", "读取", "列举", "查看", "比较",
})

# 纯文本决策界面不接受这些 Brave 多模态或上下文拼装接口。
BLOCKED_REMOTE_TOOL_NAMES = frozenset({"brave_image_search", "brave_video_search", "brave_llm_context"})

AGENT_CAPABILITIES: dict[str, frozenset[str]] = {
    AgentName.EVIDENCE_RESEARCH.value: frozenset({"web_search", "fetch_page", "place_search"}),
    AgentName.FINANCIAL_MARKET.value: frozenset({"web_search", "fetch_page", "market_data"}),
    AgentName.LOCATION_LIFESTYLE.value: frozenset({"web_search", "fetch_page", "place_search", "route_search", "weather_forecast"}),
    AgentName.PREFERENCE.value: frozenset(),
    AgentName.RISK_CRITIC.value: frozenset(),
    AgentName.PLANNER.value: frozenset(),
    AgentName.JUDGE.value: frozenset(),
    AgentName.DEBATE_MODERATOR.value: frozenset(),
    AgentName.GENERAL.value: frozenset(),
}


def _tokens(value: str) -> set[str]:
    return set(re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]+", value.casefold()))


def contains_forbidden(value: Any) -> bool:
    if isinstance(value, dict):
        return any(contains_forbidden(key) or contains_forbidden(item) for key, item in value.items())
    if isinstance(value, (list, tuple, set)):
        return any(contains_forbidden(item) for item in value)
    if not isinstance(value, str):
        return False
    normalized = value.casefold().replace("-", "_")
    tokens = _tokens(normalized)
    return any(term in normalized or term in tokens for term in FORBIDDEN_TERMS)


class ToolPolicy:
    """只授权显式配置、只读且属于当前 Agent 范围内的内部能力。"""

    def authorize(self, agent: str | AgentName, capability: str, arguments: dict[str, Any]) -> None:
        agent_name = agent.value if isinstance(agent, AgentName) else agent
        allowed = AGENT_CAPABILITIES.get(agent_name)
        if allowed is None or capability not in allowed or contains_forbidden(arguments):
            raise PermissionError("MCP operation is not permitted")

    def validate_tool(self, name: str, description: str, read_only: bool = True) -> None:
        searchable = f"{name} {description}".casefold().replace("-", "_")
        if name.casefold().replace("-", "_") in BLOCKED_REMOTE_TOOL_NAMES or not read_only or contains_forbidden(searchable):
            raise PermissionError("MCP tool is not read-only")
