"""面向工作流记忆与轨迹数据的 SQLite 仓储实现。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from models.contracts import (
    AgentResult, DecisionReport, DecisionType, Episode, Evidence, ExecutionPlan,
    ProfileMemory, ToolObservation, WorkflowEvent, WorkflowStatus,
)

from .database import SQLiteDatabase


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _to_timestamp(value: datetime | None = None) -> str:
    return (value or _utc_now()).astimezone(timezone.utc).isoformat()


def _from_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _json_dump(value: Any) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=lambda item: item.value if isinstance(item, Enum) else item.isoformat())


def _json_load(value: str | None, default: Any = None) -> Any:
    return default if value is None else json.loads(value)


@dataclass(frozen=True)
class ArchiveRecord:
    decision_id: str
    decision_type: DecisionType
    query: str
    status: WorkflowStatus
    candidates: list[str]
    constraints: list[str]
    preferences: list[str]
    plan: ExecutionPlan | None
    report: DecisionReport | None
    recommendation: str | None
    confidence: float | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class WorkingMemoryRecord:
    decision_id: str
    state: dict[str, Any]
    checkpoint_version: int
    updated_at: datetime


@dataclass(frozen=True)
class FeedbackRecord:
    id: str
    decision_id: str
    user_choice: str | None
    outcome: str | None
    notes: str | None
    created_at: datetime


@dataclass(frozen=True)
class RetrospectiveRecord:
    id: str
    decision_id: str
    result: dict[str, Any]
    created_at: datetime


class ArchiveRepository:
    def __init__(self, database: SQLiteDatabase) -> None: self.database = database

    def save(self, *, decision_id: str, decision_type: DecisionType, query: str, status: WorkflowStatus, candidates: list[str] | None = None, constraints: list[str] | None = None, preferences: list[str] | None = None, plan: ExecutionPlan | None = None, report: DecisionReport | None = None, recommendation: str | None = None, confidence: float | None = None) -> ArchiveRecord:
        now = _to_timestamp()
        with self.database.transaction() as connection:
            connection.execute("INSERT INTO decision_archives (decision_id,decision_type,query,status,candidates_json,constraints_json,preferences_json,plan_json,report_json,recommendation,confidence,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(decision_id) DO UPDATE SET decision_type=excluded.decision_type,query=excluded.query,status=excluded.status,candidates_json=excluded.candidates_json,constraints_json=excluded.constraints_json,preferences_json=excluded.preferences_json,plan_json=excluded.plan_json,report_json=excluded.report_json,recommendation=excluded.recommendation,confidence=excluded.confidence,updated_at=excluded.updated_at", (decision_id, decision_type.value, query, status.value, _json_dump(candidates or []), _json_dump(constraints or []), _json_dump(preferences or []), _json_dump(plan) if plan else None, _json_dump(report) if report else None, recommendation, confidence, now, now))
        return self.get(decision_id)  # type: ignore[return-value]

    def get(self, decision_id: str) -> ArchiveRecord | None:
        with self.database.connect() as connection: row = connection.execute("SELECT * FROM decision_archives WHERE decision_id = ?", (decision_id,)).fetchone()
        return _archive(row) if row else None

    def list(self) -> list[ArchiveRecord]:
        with self.database.connect() as connection: rows = connection.execute("SELECT * FROM decision_archives ORDER BY created_at DESC").fetchall()
        return [_archive(row) for row in rows]


    def delete_decisions(self, decision_ids: list[str], *, delete_memories: bool) -> list[str]:
        """原子删除决策产物；仅在用户明确选择时删除 Episode 与可追溯 Profile。"""
        identifiers = list(dict.fromkeys(decision_ids))
        if not identifiers:
            return []
        placeholders = ",".join("?" for _ in identifiers)
        with self.database.transaction() as connection:
            episode_rows = connection.execute(f"SELECT episode_id FROM episodes WHERE decision_id IN ({placeholders})", identifiers).fetchall()
            episode_ids = [row["episode_id"] for row in episode_rows]
            for table in ("decision_archives", "working_memories", "evidence", "agent_results", "tool_calls", "workflow_events", "feedback", "retrospectives"):
                connection.execute(f"DELETE FROM {table} WHERE decision_id IN ({placeholders})", identifiers)
            if delete_memories and episode_ids:
                episode_placeholders = ",".join("?" for _ in episode_ids)
                deleted_set = set(episode_ids)
                for profile in connection.execute("SELECT id,source_episode_ids_json FROM profile_memories").fetchall():
                    sources = _json_load(profile["source_episode_ids_json"], [])
                    remaining = [source for source in sources if source not in deleted_set]
                    if sources and not remaining:
                        connection.execute("DELETE FROM profile_memories WHERE id = ?", (profile["id"],))
                    elif len(remaining) != len(sources):
                        connection.execute("UPDATE profile_memories SET source_episode_ids_json = ? WHERE id = ?", (_json_dump(remaining), profile["id"]))
                connection.execute(f"DELETE FROM episodes WHERE episode_id IN ({episode_placeholders})", episode_ids)
        return episode_ids if delete_memories else []


class WorkingMemoryRepository:
    def __init__(self, database: SQLiteDatabase) -> None: self.database = database
    def save(self, decision_id: str, state: dict[str, Any], checkpoint_version: int) -> WorkingMemoryRecord:
        now = _to_timestamp()
        with self.database.transaction() as connection: connection.execute("INSERT INTO working_memories (decision_id,state_json,checkpoint_version,updated_at) VALUES (?,?,?,?) ON CONFLICT(decision_id) DO UPDATE SET state_json=excluded.state_json,checkpoint_version=excluded.checkpoint_version,updated_at=excluded.updated_at", (decision_id, _json_dump(state), checkpoint_version, now))
        return self.get(decision_id)  # type: ignore[return-value]
    def get(self, decision_id: str) -> WorkingMemoryRecord | None:
        with self.database.connect() as connection: row = connection.execute("SELECT * FROM working_memories WHERE decision_id = ?", (decision_id,)).fetchone()
        return None if row is None else WorkingMemoryRecord(row["decision_id"], _json_load(row["state_json"]), row["checkpoint_version"], _from_timestamp(row["updated_at"]))


class _ModelRepository:
    table: str
    id_column: str
    model: Any
    def __init__(self, database: SQLiteDatabase) -> None: self.database = database
    def get(self, identifier: str):
        with self.database.connect() as connection: row = connection.execute(_GET_QUERIES[self.table], (identifier,)).fetchone()
        return self._row_to_model(row) if row else None

    def list(self) -> list[Any]:
        with self.database.connect() as connection: rows = connection.execute(_LIST_QUERIES[self.table]).fetchall()
        return [self._row_to_model(row) for row in rows]


class EpisodeRepository(_ModelRepository):
    table, id_column, model = "episodes", "episode_id", Episode
    def save(self, item: Episode) -> Episode:
        with self.database.transaction() as c: c.execute("INSERT INTO episodes (episode_id,decision_id,decision_type,summary,options_json,recommendation,user_choice,key_reasons_json,tradeoffs_json,outcome,feedback,chosen_reason,not_chosen_reason,constraints_json,preferences_json,tags_json,profile_signals_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(episode_id) DO UPDATE SET decision_id=excluded.decision_id,decision_type=excluded.decision_type,summary=excluded.summary,options_json=excluded.options_json,recommendation=excluded.recommendation,user_choice=excluded.user_choice,key_reasons_json=excluded.key_reasons_json,tradeoffs_json=excluded.tradeoffs_json,outcome=excluded.outcome,feedback=excluded.feedback,chosen_reason=excluded.chosen_reason,not_chosen_reason=excluded.not_chosen_reason,constraints_json=excluded.constraints_json,preferences_json=excluded.preferences_json,tags_json=excluded.tags_json,profile_signals_json=excluded.profile_signals_json,created_at=excluded.created_at", (item.episode_id,item.decision_id,item.decision_type.value,item.summary,_json_dump(item.options),item.recommendation,item.user_choice,_json_dump(item.key_reasons),_json_dump(item.tradeoffs),item.outcome,item.feedback,item.chosen_reason,item.not_chosen_reason,_json_dump(item.constraints),_json_dump(item.preferences),_json_dump(item.tags),_json_dump(item.profile_signals),_to_timestamp(item.created_at)))
        return self.get(item.episode_id)  # type: ignore[return-value]
    def by_decision_id(self, decision_id: str) -> Episode | None:
        with self.database.connect() as c: r = c.execute("SELECT * FROM episodes WHERE decision_id = ? ORDER BY created_at DESC LIMIT 1", (decision_id,)).fetchone()
        return self._row_to_model(r) if r else None
    def _row_to_model(self, r): return Episode.model_validate({"episode_id":r["episode_id"],"decision_id":r["decision_id"],"decision_type":r["decision_type"],"summary":r["summary"],"options":_json_load(r["options_json"]),"recommendation":r["recommendation"],"user_choice":r["user_choice"],"key_reasons":_json_load(r["key_reasons_json"]),"tradeoffs":_json_load(r["tradeoffs_json"]),"outcome":r["outcome"],"feedback":r["feedback"],"chosen_reason":r["chosen_reason"],"not_chosen_reason":r["not_chosen_reason"],"constraints":_json_load(r["constraints_json"], []),"preferences":_json_load(r["preferences_json"], []),"tags":_json_load(r["tags_json"]),"profile_signals":_json_load(r["profile_signals_json"]),"created_at":r["created_at"]})


class ProfileRepository(_ModelRepository):
    table, id_column, model = "profile_memories", "id", ProfileMemory
    def save(self, item: ProfileMemory) -> ProfileMemory:
        with self.database.transaction() as c:
            existing = c.execute("SELECT updated_at FROM profile_memories WHERE id = ?", (item.memory_id,)).fetchone()
            updated_at = item.updated_at
            if existing is not None:
                updated_at = max(_utc_now(), _from_timestamp(existing["updated_at"]) + timedelta(microseconds=1))
            c.execute("INSERT INTO profile_memories VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET category=excluded.category,memory_key=excluded.memory_key,value_json=excluded.value_json,importance=excluded.importance,confidence=excluded.confidence,source_episode_ids_json=excluded.source_episode_ids_json,updated_at=excluded.updated_at", (item.memory_id,item.category,item.memory_key,_json_dump(item.value),item.importance,item.confidence,_json_dump(item.source_episode_ids),_to_timestamp(item.created_at),_to_timestamp(updated_at)))
        return self.get(item.memory_id)  # type: ignore[return-value]
    def _row_to_model(self, r): return ProfileMemory.model_validate({"memory_id":r["id"],"category":r["category"],"memory_key":r["memory_key"],"value":_json_load(r["value_json"]),"importance":r["importance"],"confidence":r["confidence"],"source_episode_ids":_json_load(r["source_episode_ids_json"]),"created_at":r["created_at"],"updated_at":r["updated_at"]})


class EvidenceRepository(_ModelRepository):
    table, id_column, model = "evidence", "evidence_id", Evidence
    def save(self, item: Evidence) -> Evidence:
        with self.database.transaction() as c: c.execute("INSERT INTO evidence (evidence_id,decision_id,claim,scope_key,value_json,source,source_type,agent,tool,confidence,freshness,status,supports_json,contradicts_json,retrieved_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(evidence_id) DO UPDATE SET decision_id=excluded.decision_id,claim=excluded.claim,scope_key=excluded.scope_key,value_json=excluded.value_json,source=excluded.source,source_type=excluded.source_type,agent=excluded.agent,tool=excluded.tool,confidence=excluded.confidence,freshness=excluded.freshness,status=excluded.status,supports_json=excluded.supports_json,contradicts_json=excluded.contradicts_json,retrieved_at=excluded.retrieved_at", (item.evidence_id,item.decision_id,item.claim,item.scope_key,_json_dump(item.value),item.source,item.source_type,item.agent.value if item.agent else None,item.tool,item.confidence,item.freshness,item.status.value,_json_dump(item.supports),_json_dump(item.contradicts),_to_timestamp(item.retrieved_at)))
        return self.get(item.evidence_id)  # type: ignore[return-value]
    def _row_to_model(self, r): return Evidence.model_validate({"evidence_id":r["evidence_id"],"decision_id":r["decision_id"],"claim":r["claim"],"scope_key":r["scope_key"],"value":_json_load(r["value_json"]),"source":r["source"],"source_type":r["source_type"],"agent":r["agent"],"tool":r["tool"],"confidence":r["confidence"],"freshness":r["freshness"],"status":r["status"],"supports":_json_load(r["supports_json"]),"contradicts":_json_load(r["contradicts_json"]),"retrieved_at":r["retrieved_at"]})


class AgentResultRepository(_ModelRepository):
    table, id_column, model = "agent_results", "id", AgentResult
    def save(self, item: AgentResult) -> AgentResult:
        with self.database.transaction() as c: c.execute("INSERT INTO agent_results VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET decision_id=excluded.decision_id,task_id=excluded.task_id,agent_name=excluded.agent_name,result_json=excluded.result_json,completion_status=excluded.completion_status,created_at=excluded.created_at", (item.result_id,item.decision_id,item.task_id,item.agent_name.value,_json_dump(item),item.completion_status.value,_to_timestamp(item.created_at)))
        return self.get(item.result_id)  # type: ignore[return-value]
    def _row_to_model(self, r): return AgentResult.model_validate(_json_load(r["result_json"]))


class ToolCallRepository(_ModelRepository):
    table, id_column, model = "tool_calls", "call_id", ToolObservation
    def save(self, item: ToolObservation) -> ToolObservation:
        with self.database.transaction() as c: c.execute("INSERT INTO tool_calls (call_id,decision_id,task_id,target_id,agent_name,tool_name,arguments_json,status,latency_ms,result_summary,error,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(call_id) DO UPDATE SET decision_id=excluded.decision_id,task_id=excluded.task_id,target_id=excluded.target_id,agent_name=excluded.agent_name,tool_name=excluded.tool_name,arguments_json=excluded.arguments_json,status=excluded.status,latency_ms=excluded.latency_ms,result_summary=excluded.result_summary,error=excluded.error,created_at=excluded.created_at", (item.call_id,item.decision_id,item.task_id,item.target_id,item.agent.value,item.tool_name,_json_dump(item.arguments),item.status.value,item.latency_ms,item.result_summary,item.error,_to_timestamp(item.created_at)))
        return self.get(item.call_id)  # type: ignore[return-value]
    def _row_to_model(self, r): return ToolObservation.model_validate({"call_id":r["call_id"],"decision_id":r["decision_id"],"task_id":r["task_id"],"target_id":r["target_id"],"agent":r["agent_name"],"tool_name":r["tool_name"],"arguments":_json_load(r["arguments_json"]),"status":r["status"],"latency_ms":r["latency_ms"],"result_summary":r["result_summary"],"error":r["error"],"created_at":r["created_at"]})


class TraceRepository(_ModelRepository):
    table, id_column, model = "workflow_events", "id", WorkflowEvent
    def save(self, item: WorkflowEvent) -> WorkflowEvent:
        with self.database.transaction() as c:
            c.execute(
                "INSERT INTO workflow_events (id,decision_id,from_state,to_state,kind,title,summary,sequence,payload_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET decision_id=excluded.decision_id,from_state=excluded.from_state,to_state=excluded.to_state,kind=excluded.kind,title=excluded.title,summary=excluded.summary,sequence=excluded.sequence,payload_json=excluded.payload_json,created_at=excluded.created_at",
                (item.event_id, item.decision_id, item.from_state.value if item.from_state else None,
                 item.to_state.value, item.kind, item.title, item.summary, item.sequence,
                 _json_dump(item.payload), _to_timestamp(item.created_at)),
            )
        return self.get(item.event_id)  # type: ignore[return-value]
    def list(self, decision_id: str) -> list[WorkflowEvent]:
        with self.database.connect() as c: rows = c.execute("SELECT * FROM workflow_events WHERE decision_id = ? ORDER BY sequence ASC, created_at ASC", (decision_id,)).fetchall()
        return [self._row_to_model(row) for row in rows]
    def _row_to_model(self, r): return WorkflowEvent.model_validate({"event_id":r["id"],"decision_id":r["decision_id"],"from_state":r["from_state"],"to_state":r["to_state"],"kind":r["kind"],"title":r["title"],"summary":r["summary"],"sequence":r["sequence"],"payload":_json_load(r["payload_json"]),"created_at":r["created_at"]})


class FeedbackRepository:
    def __init__(self, database: SQLiteDatabase) -> None: self.database = database
    def save(self, decision_id: str, user_choice: str | None = None, outcome: str | None = None, notes: str | None = None, id: str | None = None) -> FeedbackRecord:
        identifier, created = id or str(uuid4()), _to_timestamp()
        with self.database.transaction() as c: c.execute("INSERT INTO feedback VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET decision_id=excluded.decision_id,user_choice=excluded.user_choice,outcome=excluded.outcome,notes=excluded.notes", (identifier,decision_id,user_choice,outcome,notes,created))
        return self.get(identifier)  # type: ignore[return-value]
    def get(self, identifier: str) -> FeedbackRecord | None:
        with self.database.connect() as c: r = c.execute("SELECT * FROM feedback WHERE id = ?", (identifier,)).fetchone()
        return None if r is None else FeedbackRecord(r["id"],r["decision_id"],r["user_choice"],r["outcome"],r["notes"],_from_timestamp(r["created_at"]))
    def list(self, decision_id: str | None = None) -> list[FeedbackRecord]:
        query, parameters = ("SELECT * FROM feedback ORDER BY created_at DESC", ()) if decision_id is None else ("SELECT * FROM feedback WHERE decision_id = ? ORDER BY created_at DESC", (decision_id,))
        with self.database.connect() as c: rows = c.execute(query, parameters).fetchall()
        return [FeedbackRecord(r["id"],r["decision_id"],r["user_choice"],r["outcome"],r["notes"],_from_timestamp(r["created_at"])) for r in rows]


class RetrospectiveRepository:
    def __init__(self, database: SQLiteDatabase) -> None: self.database = database
    def save(self, decision_id: str, result: dict[str, Any], id: str | None = None) -> RetrospectiveRecord:
        identifier, created = id or str(uuid4()), _to_timestamp()
        with self.database.transaction() as c: c.execute("INSERT INTO retrospectives VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET decision_id=excluded.decision_id,result_json=excluded.result_json", (identifier,decision_id,_json_dump(result),created))
        return self.get(identifier)  # type: ignore[return-value]
    def get(self, identifier: str) -> RetrospectiveRecord | None:
        with self.database.connect() as c: r = c.execute("SELECT * FROM retrospectives WHERE id = ?", (identifier,)).fetchone()
        return None if r is None else RetrospectiveRecord(r["id"],r["decision_id"],_json_load(r["result_json"]),_from_timestamp(r["created_at"]))
    def list(self, decision_id: str | None = None) -> list[RetrospectiveRecord]:
        query, parameters = ("SELECT * FROM retrospectives ORDER BY created_at DESC", ()) if decision_id is None else ("SELECT * FROM retrospectives WHERE decision_id = ? ORDER BY created_at DESC", (decision_id,))
        with self.database.connect() as c: rows = c.execute(query, parameters).fetchall()
        return [RetrospectiveRecord(r["id"],r["decision_id"],_json_load(r["result_json"]),_from_timestamp(r["created_at"])) for r in rows]


def _archive(r: Any) -> ArchiveRecord:
    return ArchiveRecord(r["decision_id"], DecisionType(r["decision_type"]), r["query"], WorkflowStatus(r["status"]), _json_load(r["candidates_json"]), _json_load(r["constraints_json"]), _json_load(r["preferences_json"]), ExecutionPlan.model_validate(_json_load(r["plan_json"])) if r["plan_json"] else None, DecisionReport.model_validate(_json_load(r["report_json"])) if r["report_json"] else None, r["recommendation"], r["confidence"], _from_timestamp(r["created_at"]), _from_timestamp(r["updated_at"]))


_GET_QUERIES = {
    "episodes": "SELECT * FROM episodes WHERE episode_id = ?",
    "profile_memories": "SELECT * FROM profile_memories WHERE id = ?",
    "evidence": "SELECT * FROM evidence WHERE evidence_id = ?",
    "agent_results": "SELECT * FROM agent_results WHERE id = ?",
    "tool_calls": "SELECT * FROM tool_calls WHERE call_id = ?",
    "workflow_events": "SELECT * FROM workflow_events WHERE id = ?",
}

_LIST_QUERIES = {
    "episodes": "SELECT * FROM episodes ORDER BY created_at DESC",
    "profile_memories": "SELECT * FROM profile_memories ORDER BY created_at DESC",
    "evidence": "SELECT * FROM evidence ORDER BY retrieved_at DESC",
    "agent_results": "SELECT * FROM agent_results ORDER BY created_at DESC",
    "tool_calls": "SELECT * FROM tool_calls ORDER BY created_at DESC",
    "workflow_events": "SELECT * FROM workflow_events ORDER BY created_at DESC",
}
