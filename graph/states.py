"""可持久化的决策工作流状态。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from models.contracts import (
    AgentName, AgentResult, DecisionReport, DecisionRequest, DecisionType,
    ExecutionPlan, HITLRequest, MemoryContext, ReplanDecision, ToolObservation, WorkflowStatus,
)


class DecisionState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision_id: str
    request: DecisionRequest
    decision_type: DecisionType = DecisionType.GENERAL
    status: WorkflowStatus = WorkflowStatus.RECEIVED
    skill_name: str | None = None
    memory: MemoryContext = Field(default_factory=MemoryContext)
    plan: ExecutionPlan | None = None
    agent_results: list[AgentResult] = Field(default_factory=list)
    report: DecisionReport | None = None
    activated_agents: list[AgentName] = Field(default_factory=list)
    replan_count: int = 0
    checkpoint_version: int = 0
    fallback_events: list[dict] = Field(default_factory=list)
    hitl_requests: list[HITLRequest] = Field(default_factory=list)
    tool_observations: list[ToolObservation] = Field(default_factory=list)
    progress_summaries: list[dict] = Field(default_factory=list)
    task_ledger: dict[str, dict[str, Any]] = Field(default_factory=dict)
    information_coverage: dict[str, dict[str, Any]] = Field(default_factory=dict)
    information_targets: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    replan_decision: ReplanDecision | None = None
