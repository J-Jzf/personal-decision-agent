import asyncio

import pytest

from mcp.gateway import MCPGateway
from mcp.policy import ToolPolicy
from mcp.registry import ToolRegistry


class FakeSession:
    def __init__(self, tools=None, result=None, failures=0):
        self.tools = tools or []
        self.result = result or {"content": [{"type": "text", "text": "ok"}]}
        self.failures = failures
        self.calls = 0

    async def list_tools(self):
        return {"tools": self.tools}

    async def call_tool(self, name, arguments):
        self.calls += 1
        if self.calls <= self.failures:
            raise TimeoutError("temporary timeout")
        return self.result


def test_registry_excludes_non_text_brave_tools_from_discovery():
    """纯文本决策不应把图片、视频和上下文生成工具暴露给专家。"""
    async def scenario():
        session = FakeSession(tools=[
            {"name": "brave_web_search", "description": "Search the web", "inputSchema": {"type": "object"}},
            {"name": "brave_image_search", "description": "Search images", "inputSchema": {"type": "object"}},
            {"name": "brave_video_search", "description": "Search video", "inputSchema": {"type": "object"}},
            {"name": "brave_llm_context", "description": "Build LLM context", "inputSchema": {"type": "object"}},
        ])
        gateway = MCPGateway(registry=ToolRegistry(), sessions={"brave": session})

        discovered = await gateway.discover()

        assert [tool.name for tool in discovered] == ["brave_web_search"]

    asyncio.run(scenario())


def test_registry_exposes_only_tavily_search_not_tavily_research():
    """深度研究工具容易超时，专家只能看到快速的 Tavily 搜索工具。"""
    async def scenario():
        session = FakeSession(tools=[
            {"name": "tavily_search", "description": "Search the web", "inputSchema": {"type": "object"}},
            {"name": "tavily_research", "description": "Run deep research", "inputSchema": {"type": "object"}},
        ])
        gateway = MCPGateway(registry=ToolRegistry(), sessions={"tavily": session})

        discovered = await gateway.discover()

        assert [tool.name for tool in discovered] == ["tavily_search"]

    asyncio.run(scenario())


def test_gateway_invokes_only_the_explicitly_selected_remote_tool():
    """专家选择具体工具后，网关不得把同一参数广播给同能力的其他工具。"""
    async def scenario():
        session = FakeSession(tools=[
            {"name": "brave_web_search", "description": "Search web", "inputSchema": {"type": "object", "required": ["query"]}},
            {"name": "brave_summarizer", "description": "Search summary", "inputSchema": {"type": "object", "required": ["key"]}},
        ])
        gateway = MCPGateway(registry=ToolRegistry(), sessions={"brave": session})
        await gateway.discover()

        result = await gateway.call_tool(
            "evidence_research", "brave_web_search", {"query": "上海周末天气"}, decision_id="d", task_id="t"
        )

        assert result.status.value == "succeeded"
        assert result.tool_name == "brave_web_search"
        assert session.calls == 1

    asyncio.run(scenario())


def test_gateway_exposes_only_explicit_tool_invocation_api():
    """网关不能再提供按能力名猜测工具的旧调用入口。"""
    assert not hasattr(MCPGateway, "call")


def test_policy_blocks_forbidden_nested_arguments_and_unknown_agent():
    policy = ToolPolicy()
    with pytest.raises(PermissionError):
        policy.authorize("financial_market", "market_data", {"order": {"action": "buy"}})
    with pytest.raises(PermissionError):
        policy.authorize("unknown", "web_search", {"query": "safe"})


def test_gateway_discovers_maps_retries_audits_and_degrades():
    async def scenario():
        session = FakeSession(
            tools=[{"name": "search_web", "description": "Search the web", "inputSchema": {"type": "object"}}],
            failures=1,
        )
        audit = []
        gateway = MCPGateway(registry=ToolRegistry(), sessions={"local": session}, audit_sink=audit.append)
        discovered = await gateway.discover()
        assert discovered[0].capability == "web_search"
        result = await gateway.call_tool("evidence_research", "search_web", {"query": "杭州"}, decision_id="d", task_id="t")
        assert result.status.value == "succeeded"
        assert session.calls == 2
        assert len(audit) == 1
        missing = await gateway.call_tool("location_lifestyle", "weather_forecast", {"city": "杭州"}, decision_id="d", task_id="t")
        assert missing.status.value == "unavailable"

    asyncio.run(scenario())


def test_gateway_enforces_task_call_limit_before_external_invocation():
    async def scenario():
        session = FakeSession(tools=[{"name": "search_web", "description": "Search web", "inputSchema": {}}])
        audit = []
        gateway = MCPGateway(registry=ToolRegistry(), sessions={"local": session}, audit_sink=audit.append, max_calls_per_task=3)
        await gateway.discover()
        for _ in range(3):
            await gateway.call_tool("evidence_research", "search_web", {"query": "safe"}, decision_id="d", task_id="t")
        exhausted = await gateway.call_tool("evidence_research", "search_web", {"query": "safe"}, decision_id="d", task_id="t")
        assert session.calls == 3
        assert exhausted.status.value == "denied"
        assert "调用额度耗尽" in (exhausted.error or "")
        assert audit[-1].status.value == "denied"

    asyncio.run(scenario())


def test_gateway_rejects_tool_arguments_that_violate_discovered_schema_before_calling_mcp():
    """fetch_page 缺少 url 时必须本地失败，不能把错误文本伪装成证据。"""
    async def scenario():
        session = FakeSession(tools=[{
            "name": "fetch", "description": "Fetch a URL",
            "inputSchema": {"type": "object", "required": ["url"], "properties": {"url": {"type": "string"}}},
        }])
        gateway = MCPGateway(registry=ToolRegistry(), sessions={"fetch": session})
        await gateway.discover()

        result = await gateway.call_tool("evidence_research", "fetch", {"query": "上海天气"}, decision_id="d", task_id="t")

        assert result.status.value == "failed"
        assert "url" in (result.error or "")
        assert session.calls == 0

    asyncio.run(scenario())


def test_gateway_marks_textual_mcp_validation_errors_as_failed_observations():
    """部分 MCP 将校验错误放在 content 文本内，网关仍必须识别为失败。"""
    async def scenario():
        session = FakeSession(
            tools=[{"name": "search_web", "description": "Search web", "inputSchema": {"type": "object"}}],
            result={"content": [{"type": "text", "text": "Input validation error: 'query' is required property"}]},
        )
        gateway = MCPGateway(registry=ToolRegistry(), sessions={"search": session})
        await gateway.discover()

        result = await gateway.call_tool("evidence_research", "search_web", {"query": "上海"}, decision_id="d", task_id="t")

        assert result.status.value == "failed"
        assert "Input validation error" in (result.error or "")
        assert "search" in (result.error or "")

    asyncio.run(scenario())
