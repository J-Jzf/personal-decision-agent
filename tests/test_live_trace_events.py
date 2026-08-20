"""验证实时轨迹事件能够安全地写入并从 SQLite 读取。"""

from datetime import datetime, timezone

from memory.database import SQLiteDatabase
from memory.repositories import TraceRepository
from models.contracts import WorkflowEvent, WorkflowStatus


def test_trace_repository_preserves_structured_live_event_fields(tmp_path):
    """实时工具观察事件的展示字段应在持久化后保持不变。"""
    repository = TraceRepository(SQLiteDatabase(tmp_path / "trace.sqlite3"))
    event = WorkflowEvent(
        event_id="event-1",
        decision_id="decision-1",
        from_state=WorkflowStatus.EXECUTING,
        to_state=WorkflowStatus.EXECUTING,
        kind="tool_observation",
        title="工具返回结果",
        summary="天气服务已返回上海周末降雨概率。",
        sequence=3,
        payload={"tool": "weather_forecast"},
        created_at=datetime(2026, 8, 18, tzinfo=timezone.utc),
    )

    repository.save(event)

    restored = repository.list("decision-1")
    assert restored == [event]


def test_trace_event_uses_backward_compatible_transition_defaults():
    """旧调用方未提供展示字段时仍应生成可显示的默认轨迹事件。"""
    event = WorkflowEvent(
        event_id="event-2",
        decision_id="decision-1",
        to_state=WorkflowStatus.PLANNED,
    )

    assert event.kind == "workflow_transition"
    assert event.sequence == 0


def test_trace_repository_preserves_public_evidence_references_for_a_derived_target(tmp_path):
    """前端应能看到归纳目标引用了哪些已核验资料，但不需要暴露模型私有思维。"""
    repository = TraceRepository(SQLiteDatabase(tmp_path / "trace.sqlite3"))
    event = WorkflowEvent(
        event_id="event-derived", decision_id="decision-1",
        from_state=WorkflowStatus.EXECUTING, to_state=WorkflowStatus.EXECUTING,
        kind="information_target_resolved", title="专家已结算信息目标", summary="基于已有资料完成比较。",
        sequence=4,
        payload={"resolution": {
            "target_id": "comparison", "status": "complete",
            "evidence_refs": ["call-a", "call-b"], "reasoning_basis": "conservative_inference",
        }},
    )

    repository.save(event)

    restored = repository.list("decision-1")[0]
    assert restored.payload["resolution"]["evidence_refs"] == ["call-a", "call-b"]
    assert restored.payload["resolution"]["reasoning_basis"] == "conservative_inference"
