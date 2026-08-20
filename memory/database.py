"""为持久化工作流记忆提供 SQLite 初始化与事务支持。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class SQLiteDatabase:
    """持有 SQLite 数据库路径，并负责创建全部持久化表结构。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connect() as connection:
            yield connection

    def initialize(self) -> None:
        statements = (
            "CREATE TABLE IF NOT EXISTS decision_archives (decision_id TEXT PRIMARY KEY, decision_type TEXT NOT NULL, query TEXT NOT NULL, status TEXT NOT NULL, candidates_json TEXT NOT NULL, constraints_json TEXT NOT NULL, preferences_json TEXT NOT NULL, plan_json TEXT, report_json TEXT, recommendation TEXT, confidence REAL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS working_memories (decision_id TEXT PRIMARY KEY, state_json TEXT NOT NULL, checkpoint_version INTEGER NOT NULL, updated_at TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS episodes (episode_id TEXT PRIMARY KEY, decision_id TEXT NOT NULL, decision_type TEXT NOT NULL, summary TEXT NOT NULL, options_json TEXT NOT NULL, recommendation TEXT, user_choice TEXT, key_reasons_json TEXT NOT NULL, tradeoffs_json TEXT NOT NULL, outcome TEXT, feedback TEXT, chosen_reason TEXT, not_chosen_reason TEXT, constraints_json TEXT NOT NULL DEFAULT '[]', preferences_json TEXT NOT NULL DEFAULT '[]', tags_json TEXT NOT NULL, profile_signals_json TEXT NOT NULL, created_at TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS profile_memories (id TEXT PRIMARY KEY, category TEXT NOT NULL, memory_key TEXT NOT NULL, value_json TEXT NOT NULL, importance REAL NOT NULL, confidence REAL NOT NULL, source_episode_ids_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS evidence (evidence_id TEXT PRIMARY KEY, decision_id TEXT NOT NULL, claim TEXT NOT NULL, scope_key TEXT, value_json TEXT NOT NULL, source TEXT, source_type TEXT, agent TEXT, tool TEXT, confidence REAL NOT NULL, freshness REAL, status TEXT NOT NULL, supports_json TEXT NOT NULL, contradicts_json TEXT NOT NULL, retrieved_at TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS agent_results (id TEXT PRIMARY KEY, decision_id TEXT NOT NULL, task_id TEXT NOT NULL, agent_name TEXT NOT NULL, result_json TEXT NOT NULL, completion_status TEXT NOT NULL, created_at TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS tool_calls (call_id TEXT PRIMARY KEY, decision_id TEXT NOT NULL, task_id TEXT NOT NULL, target_id TEXT, agent_name TEXT NOT NULL, tool_name TEXT NOT NULL, arguments_json TEXT NOT NULL, status TEXT NOT NULL, latency_ms INTEGER, result_summary TEXT, error TEXT, created_at TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS workflow_events (id TEXT PRIMARY KEY, decision_id TEXT NOT NULL, from_state TEXT, to_state TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'workflow_transition', title TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL DEFAULT '', sequence INTEGER NOT NULL DEFAULT 0, payload_json TEXT NOT NULL, created_at TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS feedback (id TEXT PRIMARY KEY, decision_id TEXT NOT NULL, user_choice TEXT, outcome TEXT, notes TEXT, created_at TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS retrospectives (id TEXT PRIMARY KEY, decision_id TEXT NOT NULL, result_json TEXT NOT NULL, created_at TEXT NOT NULL)",
        )
        with self.transaction() as connection:
            for statement in statements:
                connection.execute(statement)
            # 旧数据库已含 workflow_events 表；逐列迁移使升级不会丢失历史轨迹。
            existing_columns = {row[1] for row in connection.execute("PRAGMA table_info(workflow_events)")}
            for name, definition in (
                ("kind", "TEXT NOT NULL DEFAULT 'workflow_transition'"),
                ("title", "TEXT NOT NULL DEFAULT ''"),
                ("summary", "TEXT NOT NULL DEFAULT ''"),
                ("sequence", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if name not in existing_columns:
                    connection.execute(f"ALTER TABLE workflow_events ADD COLUMN {name} {definition}")
            episode_columns = {row[1] for row in connection.execute("PRAGMA table_info(episodes)")}
            for name, definition in (("chosen_reason", "TEXT"), ("not_chosen_reason", "TEXT"), ("constraints_json", "TEXT NOT NULL DEFAULT '[]'"), ("preferences_json", "TEXT NOT NULL DEFAULT '[]'")):
                if name not in episode_columns:
                    connection.execute(f"ALTER TABLE episodes ADD COLUMN {name} {definition}")
            evidence_columns = {row[1] for row in connection.execute("PRAGMA table_info(evidence)")}
            if "scope_key" not in evidence_columns:
                connection.execute("ALTER TABLE evidence ADD COLUMN scope_key TEXT")
            tool_call_columns = {row[1] for row in connection.execute("PRAGMA table_info(tool_calls)")}
            if "target_id" not in tool_call_columns:
                connection.execute("ALTER TABLE tool_calls ADD COLUMN target_id TEXT")
