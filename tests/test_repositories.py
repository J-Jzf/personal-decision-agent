from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from memory.database import SQLiteDatabase
from memory.repositories import (
    AgentResultRepository,
    ArchiveRepository,
    EpisodeRepository,
    EvidenceRepository,
    FeedbackRepository,
    ProfileRepository,
    RetrospectiveRepository,
    ToolCallRepository,
    TraceRepository,
    WorkingMemoryRepository,
)
from models.contracts import (
    AgentName,
    AgentResult,
    DecisionReport,
    DecisionType,
    Episode,
    Evidence,
    EvidenceStatus,
    ExecutionPlan,
    ProfileMemory,
    TaskStatus,
    ToolCallStatus,
    ToolObservation,
    WorkflowEvent,
    WorkflowStatus,
)


def _database(tmp_path):
    return SQLiteDatabase(tmp_path / "nested" / "memory.sqlite3")


def test_archive_create_get_list_and_preserves_id_on_update(tmp_path):
    database = _database(tmp_path)
    repository = ArchiveRepository(database)
    created = repository.save(
        decision_id="d-1", decision_type=DecisionType.PRODUCT, query="Choose a laptop",
        status=WorkflowStatus.PLANNED, candidates=["A", "B"], constraints=["budget"],
        preferences=["battery"], plan=ExecutionPlan(goal="choose"),
        report=DecisionReport(recommended_option="A", confidence=0.8),
    )
    updated = repository.save(
        decision_id="d-1", decision_type=DecisionType.PRODUCT, query="Choose a laptop",
        status=WorkflowStatus.COMPLETED, candidates=["A", "B"], recommendation="A", confidence=0.9,
    )
    assert created.decision_id == "d-1"
    assert updated.status is WorkflowStatus.COMPLETED
    assert updated.created_at == created.created_at
    assert repository.get("missing") is None
    assert repository.get("d-1").candidates == ["A", "B"]
    assert [item.decision_id for item in repository.list()] == ["d-1"]


def test_working_memory_upsert_and_missing_value(tmp_path):
    repository = WorkingMemoryRepository(_database(tmp_path))
    assert repository.get("none") is None
    first = repository.save("d-1", {"step": 1}, checkpoint_version=1)
    second = repository.save("d-1", {"step": 2}, checkpoint_version=2)
    assert first.decision_id == second.decision_id == "d-1"
    assert repository.get("d-1").state == {"step": 2}
    assert repository.get("d-1").checkpoint_version == 2


def test_profile_upsert_preserves_created_at_and_refreshes_updated_at(tmp_path):
    repository = ProfileRepository(_database(tmp_path))
    original = ProfileMemory(
        memory_id="p-1", category="work", memory_key="remote", value=True,
        importance=0.7, confidence=0.8,
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    first = repository.save(original)
    second = repository.save(original)
    assert second.created_at == first.created_at
    assert second.updated_at > first.updated_at


def test_domain_models_round_trip_through_all_persistence_tables(tmp_path):
    database = _database(tmp_path)
    timestamp = datetime(2026, 1, 2, tzinfo=timezone.utc)
    episode = Episode(episode_id="e-1", decision_id="d-1", decision_type=DecisionType.TRAVEL, summary="Trip choice", options=["Tokyo"], tags=["city"], created_at=timestamp)
    profile = ProfileMemory(memory_id="p-1", category="travel", memory_key="pace", value={"value": "slow"}, importance=0.8, confidence=0.9, source_episode_ids=["e-1"], created_at=timestamp, updated_at=timestamp)
    evidence = Evidence(evidence_id="v-1", decision_id="d-1", claim="Tokyo is safe", value={"score": 8}, agent=AgentName.EVIDENCE_RESEARCH, status=EvidenceStatus.CONFIRMED, supports=["v-2"], retrieved_at=timestamp)
    result = AgentResult(result_id="r-1", decision_id="d-1", task_id="t-1", agent_name=AgentName.PLANNER, summary="done", evidence_ids=["v-1"], completion_status=TaskStatus.COMPLETED, created_at=timestamp)
    call = ToolObservation(call_id="c-1", decision_id="d-1", task_id="t-1", agent=AgentName.PLANNER, tool_name="search", arguments={"q": "Tokyo"}, status=ToolCallStatus.SUCCEEDED, latency_ms=12, result_summary="found", created_at=timestamp)
    event = WorkflowEvent(event_id="w-1", decision_id="d-1", from_state=WorkflowStatus.PLANNED, to_state=WorkflowStatus.EXECUTING, payload={"attempt": 1}, created_at=timestamp)
    EpisodeRepository(database).save(episode)
    ProfileRepository(database).save(profile)
    EvidenceRepository(database).save(evidence)
    AgentResultRepository(database).save(result)
    ToolCallRepository(database).save(call)
    TraceRepository(database).save(event)
    assert EpisodeRepository(database).get("e-1") == episode
    assert ProfileRepository(database).get("p-1") == profile
    assert EvidenceRepository(database).get("v-1") == evidence
    assert AgentResultRepository(database).get("r-1") == result
    assert ToolCallRepository(database).get("c-1") == call
    assert TraceRepository(database).list("d-1") == [event]


def test_trace_order_feedback_and_retrospective_round_trips(tmp_path):
    database = _database(tmp_path)
    trace = TraceRepository(database)
    trace.save(WorkflowEvent(event_id="old", decision_id="d-1", to_state=WorkflowStatus.RECEIVED, created_at=datetime(2025, 1, 1, tzinfo=timezone.utc)))
    trace.save(WorkflowEvent(event_id="new", decision_id="d-1", from_state=WorkflowStatus.RECEIVED, to_state=WorkflowStatus.PLANNED, created_at=datetime(2025, 1, 2, tzinfo=timezone.utc)))
    feedback = FeedbackRepository(database).save("d-1", user_choice="A", notes="good")
    retrospective = RetrospectiveRepository(database).save("d-1", {"lesson": "verify pricing"})
    assert [event.event_id for event in trace.list("d-1")] == ["old", "new"]
    assert FeedbackRepository(database).get(feedback.id).notes == "good"
    assert RetrospectiveRepository(database).get(retrospective.id).result == {"lesson": "verify pricing"}
    assert FeedbackRepository(database).get("missing") is None
    assert RetrospectiveRepository(database).get("missing") is None


def test_database_initializes_required_schema_columns(tmp_path):
    database = _database(tmp_path)
    required = {
        "decision_archives": {"decision_id", "decision_type", "query", "status", "candidates_json", "constraints_json", "preferences_json", "plan_json", "report_json", "recommendation", "confidence", "created_at", "updated_at"},
        "working_memories": {"decision_id", "state_json", "checkpoint_version", "updated_at"},
        "episodes": {"episode_id", "decision_id", "decision_type", "summary", "options_json", "recommendation", "user_choice", "key_reasons_json", "tradeoffs_json", "outcome", "feedback", "tags_json", "profile_signals_json", "created_at"},
        "profile_memories": {"id", "category", "memory_key", "value_json", "importance", "confidence", "source_episode_ids_json", "created_at", "updated_at"},
        "evidence": {"evidence_id", "decision_id", "claim", "scope_key", "value_json", "source", "source_type", "agent", "tool", "confidence", "freshness", "status", "supports_json", "contradicts_json", "retrieved_at"},
        "agent_results": {"id", "decision_id", "task_id", "agent_name", "result_json", "completion_status", "created_at"},
        "tool_calls": {"call_id", "decision_id", "task_id", "target_id", "agent_name", "tool_name", "arguments_json", "status", "latency_ms", "result_summary", "error", "created_at"},
        "workflow_events": {"id", "decision_id", "from_state", "to_state", "payload_json", "created_at"},
        "feedback": {"id", "decision_id", "user_choice", "outcome", "notes", "created_at"},
        "retrospectives": {"id", "decision_id", "result_json", "created_at"},
    }
    with sqlite3.connect(database.path) as connection:
        for table, expected_columns in required.items():
            actual_columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
            assert expected_columns <= actual_columns


def test_deleting_decision_memories_removes_isolated_profiles_and_keeps_shared_sources(tmp_path):
    """用户选择删除关联记忆时，只能移除完全由该决策支撑的长期偏好。"""
    database = _database(tmp_path)
    archives, episodes, profiles = ArchiveRepository(database), EpisodeRepository(database), ProfileRepository(database)
    archives.save(decision_id="d-1", decision_type=DecisionType.TRAVEL, query="旅行", status=WorkflowStatus.ARCHIVED)
    episodes.save(Episode(episode_id="e-1", decision_id="d-1", decision_type=DecisionType.TRAVEL, summary="旅行记录"))
    episodes.save(Episode(episode_id="e-2", decision_id="d-2", decision_type=DecisionType.TRAVEL, summary="另一条记录"))
    profiles.save(ProfileMemory(memory_id="only", category="travel", memory_key="pace", value="slow", importance=.7, confidence=.8, source_episode_ids=["e-1"]))
    profiles.save(ProfileMemory(memory_id="shared", category="travel", memory_key="food", value="local", importance=.7, confidence=.8, source_episode_ids=["e-1", "e-2"]))

    deleted = archives.delete_decisions(["d-1"], delete_memories=True)

    assert deleted == ["e-1"]
    assert archives.get("d-1") is None
    assert episodes.get("e-1") is None
    assert profiles.get("only") is None
    assert profiles.get("shared").source_episode_ids == ["e-2"]
