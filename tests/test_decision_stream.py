"""验证决策 API 以 SSE 实时返回可审计的执行轨迹。"""

import json

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.trace_stream import sanitize_trace_value


def _settings(tmp_path, name: str) -> Settings:
    """创建不读取真实模型服务的确定性流式 API 测试配置。"""
    return Settings(
        _env_file=None,
        SQLITE_PATH=tmp_path / f"{name}.sqlite", QDRANT_PATH=tmp_path / f"{name}-qdrant",
        LLM_MODEL_ID=None, LLM_API_KEY=None, LLM_BASE_URL=None, MCP_COMMANDS_JSON=[],
    )


def _sse_messages(body: str) -> list[tuple[str, dict]]:
    """将测试响应中的 SSE 文本解码为事件名与 JSON 数据。"""
    messages: list[tuple[str, dict]] = []
    for block in body.split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        if "event" in lines and "data" in lines:
            messages.append((lines["event"], json.loads(lines["data"])))
    return messages


def test_decision_stream_emits_auditable_plan_agent_tool_and_completion_events(tmp_path):
    """流式决策应先返回执行标识，随后逐步给出计划、专家和工具轨迹。"""
    settings = _settings(tmp_path, "stream")
    with TestClient(create_app(settings)) as client:
        response = client.post("/decision/stream", json={"query": "上海和贵州周末旅游怎么选"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    messages = _sse_messages(response.text)
    names = [name for name, _ in messages]
    assert names[0] == "decision_started"
    assert "plan_created" in names
    assert "agent_task_started" in names
    # 模型不可用时会安全降级为本地分析：不应伪造 ReAct 或 MCP 工具观察。
    assert "agent_task_completed" in names
    assert names[-1] == "decision_completed"
    sequences = [data["sequence"] for name, data in messages if name != "decision_completed"]
    assert sequences == sorted(sequences)
    assert messages[-1][1]["response"]["report"] is not None


def test_stream_sanitizer_redacts_sensitive_arguments_and_truncates_verbose_values():
    """实时轨迹的参数不能泄漏凭据，超长工具结果也必须截断。"""
    safe = sanitize_trace_value({"authorization": "Bearer private", "nested": {"api_key": "private"}, "result": "x" * 1501})

    assert safe["authorization"] == "***"
    assert safe["nested"]["api_key"] == "***"
    assert safe["result"].endswith("…（已截断）")
