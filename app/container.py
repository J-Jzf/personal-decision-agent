"""应用依赖装配层；唯一负责连接存储、模型和 MCP 边界对象的模块。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agents.judge import DecisionJudge
from agents.planner import Planner
from app.config import Settings
from graph.decision_graph import DecisionGraph
from llm.adapter import ModelAdapter
from mcp.gateway import MCPGateway
from memory.database import SQLiteDatabase
from memory.manager import MemoryManager
from memory.repositories import (
    AgentResultRepository, ArchiveRepository, EpisodeRepository, EvidenceRepository,
    FeedbackRepository, ProfileRepository, RetrospectiveRepository, ToolCallRepository,
    TraceRepository, WorkingMemoryRepository,
)
from memory.vector_index import LocalEpisodeIndex
from skills.registry import SkillRegistry


@dataclass
class Services:
    settings: Settings
    database: SQLiteDatabase
    memory: MemoryManager
    skills: SkillRegistry
    gateway: MCPGateway
    graph: DecisionGraph
    archives: ArchiveRepository
    working: WorkingMemoryRepository
    evidence: EvidenceRepository
    agent_results: AgentResultRepository
    tool_calls: ToolCallRepository
    traces: TraceRepository
    feedback: FeedbackRepository
    retrospectives: RetrospectiveRepository


def build_services(settings: Settings | None = None) -> Services:
    settings = settings or Settings()
    database = SQLiteDatabase(settings.sqlite_path)
    archives, working = ArchiveRepository(database), WorkingMemoryRepository(database)
    episodes, profiles = EpisodeRepository(database), ProfileRepository(database)
    evidence, agent_results = EvidenceRepository(database), AgentResultRepository(database)
    tool_calls, traces = ToolCallRepository(database), TraceRepository(database)
    feedback, retrospectives = FeedbackRepository(database), RetrospectiveRepository(database)
    memory = MemoryManager(episodes, profiles, LocalEpisodeIndex(settings.qdrant_path))
    skills = SkillRegistry(Path(__file__).resolve().parents[1] / "skills"); skills.load_all()
    gateway = MCPGateway.from_commands(
        settings.mcp_commands, audit_sink=tool_calls,
        timeout_seconds=settings.tool_timeout_seconds,
        max_calls_per_task=settings.react_call_limit,
    )
    adapter = ModelAdapter(settings)
    graph = DecisionGraph(
        planner=Planner(adapter), judge=DecisionJudge(adapter), memory=memory,
        skills=skills, gateway=gateway, archives=archives, working=working,
        evidence_repository=evidence, agent_results_repository=agent_results, traces=traces,
    )
    return Services(settings, database, memory, skills, gateway, graph, archives, working,
                    evidence, agent_results, tool_calls, traces, feedback, retrospectives)
