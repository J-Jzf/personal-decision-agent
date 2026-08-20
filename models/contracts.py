"""定义请求、工作流状态、证据和决策结果的 Pydantic 跨层契约。"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _validate_identifier_list(values: list[str]) -> list[str]:
    if any(not value for value in values):
        raise ValueError("identifiers must be non-empty")
    return values


class DecisionType(str, Enum):
    JOB_OFFER = "job_offer"
    PRODUCT = "product"
    TRAVEL = "travel"
    PORTFOLIO = "portfolio"
    COURSE_SUBSCRIPTION = "course_subscription"
    GENERAL = "general"


class WorkflowStatus(str, Enum):
    RECEIVED = "received"
    CLASSIFIED = "classified"
    MEMORY_RETRIEVED = "memory_retrieved"
    SKILL_LOADED = "skill_loaded"
    PLANNED = "planned"
    EXECUTING = "executing"
    WAITING_FOR_INPUT = "waiting_for_input"
    REPLANNING = "replanning"
    VERIFYING = "verifying"
    DEBATING = "debating"
    JUDGING = "judging"
    COMPLETED = "completed"
    ARCHIVED = "archived"
    FAILED = "failed"


class EvidenceStatus(str, Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    UNVERIFIED = "unverified"
    CONFLICTING = "conflicting"
    REJECTED = "rejected"
    UNAVAILABLE = "unavailable"


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    # 已有可用于保守结论的资料，但仍保留不会阻止后续判断的公开缺口。
    COMPLETED_WITH_GAPS = "completed_with_gaps"
    FAILED = "failed"
    BLOCKED = "blocked"
    SKIPPED = "skipped"


class AgentName(str, Enum):
    PLANNER = "planner"
    EVIDENCE_RESEARCH = "evidence_research"
    FINANCIAL_MARKET = "financial_market"
    LOCATION_LIFESTYLE = "location_lifestyle"
    PREFERENCE = "preference"
    RISK_CRITIC = "risk_critic"
    JUDGE = "judge"
    DEBATE_MODERATOR = "debate_moderator"
    GENERAL = "general"


class ToolCallStatus(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DENIED = "denied"
    UNAVAILABLE = "unavailable"
    TIMED_OUT = "timed_out"


class ObservationRelevance(str, Enum):
    """描述成功调用的内容是否在语义上支撑当前信息目标。"""

    RELEVANT = "relevant"
    PARTIAL = "partial"
    IRRELEVANT = "irrelevant"
    UNVERIFIABLE = "unverifiable"


class AnalysisMode(str, Enum):
    """标识报告由模型主导还是在模型不可用后由本地规则生成。"""

    MODEL = "model"
    DETERMINISTIC_FALLBACK = "deterministic_fallback"


class HITLStatus(str, Enum):
    """记录一次人工补充请求的生命周期，供恢复工作流和前端倒计时使用。"""

    PENDING = "pending"
    ANSWERED = "answered"
    SKIPPED = "skipped"
    TIMED_OUT = "timed_out"


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class TaskSpec(ContractModel):
    task_id: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    agent: AgentName = AgentName.EVIDENCE_RESEARCH
    dependencies: list[str] = Field(default_factory=list)
    required_capabilities: list[str] = Field(default_factory=list)
    completion_criteria: list[str] = Field(default_factory=list)
    status: TaskStatus = TaskStatus.PENDING

    @property
    def assigned_agent(self) -> str:
        """供 API 与诊断使用的稳定 Planner 视图别名。"""
        return self.agent.value

    @field_validator("dependencies")
    @classmethod
    def dependencies_are_nonempty_and_unique(cls, values: list[str]) -> list[str]:
        if any(not value for value in values):
            raise ValueError("dependency identifiers must be non-empty")
        if len(values) != len(set(values)):
            raise ValueError("dependency identifiers must be unique")
        return values


class ExecutionPlan(ContractModel):
    goal: str = "decision analysis"
    tasks: list[TaskSpec] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    requires_verification: bool = False
    requires_debate: bool = False
    replan_conditions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def task_graph_is_valid(self) -> "ExecutionPlan":
        task_ids = [task.task_id for task in self.tasks]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("task IDs must be unique")
        known_ids = set(task_ids)
        for task in self.tasks:
            unknown = set(task.dependencies) - known_ids
            if unknown:
                raise ValueError(f"task {task.task_id} depends on unknown task IDs: {sorted(unknown)}")
            if task.task_id in task.dependencies:
                raise ValueError(f"task {task.task_id} cannot depend on itself")
        return self


class AutonomousPlan(ContractModel):
    """由总控模型选择领域、Skill 和任务 DAG 的公开可审计规划结果。"""

    decision_type: DecisionType = DecisionType.GENERAL
    skill_name: str | None = None
    planning_summary: str = Field(min_length=1)
    plan: ExecutionPlan
    hitl_question: str | None = None
    hitl_rationale: str | None = None
    hitl_fields: list[HITLField] = Field(default_factory=list, max_length=3)


class HITLField(ContractModel):
    """用户补充信息表单中的一个可选或必填字段。"""

    key: str = Field(min_length=1, max_length=80)
    label: str = Field(min_length=1, max_length=120)
    placeholder: str = Field(default="", max_length=240)
    required: bool = False


class HITLRequest(ContractModel):
    """总控或专家发出的公开人工补充请求，不承载私有推理内容。"""

    request_id: str = Field(min_length=1)
    decision_id: str = Field(min_length=1)
    source_agent: AgentName
    stage: Literal["planning", "execution", "replanning"]
    question: str = Field(min_length=1, max_length=600)
    rationale: str = Field(min_length=1, max_length=600)
    fields: list[HITLField] = Field(default_factory=list, max_length=3)
    status: HITLStatus = HITLStatus.PENDING
    response_values: dict[str, str] = Field(default_factory=dict)
    free_text: str = Field(default="", max_length=2000)


class HITLResponse(ContractModel):
    """浏览器对人工补充请求提交的字段值或明确跳过选择。"""

    values: dict[str, str] = Field(default_factory=dict)
    free_text: str = Field(default="", max_length=2000)
    skip: bool = False


class DeleteDecisionsRequest(ContractModel):
    """删除一个前端对话关联的全部决策归档，并可选择同步清除可追溯记忆。"""

    decision_ids: list[str] = Field(min_length=1)
    delete_memories: bool = False

    @field_validator("decision_ids")
    @classmethod
    def decision_ids_are_unique(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(_validate_identifier_list(values)))


class InformationCoverageUpdate(ContractModel):
    """专家基于已观察结果更新的信息目标覆盖状态，不包含模型私有推理。"""

    target_key: str = Field(min_length=1, max_length=120, pattern=r"^[a-z0-9][a-z0-9._:-]*$")
    target: str = Field(min_length=1, max_length=240)
    status: Literal["partial", "complete"]
    summary: str = Field(min_length=1, max_length=1000)


class TargetResolution(ContractModel):
    """专家结束当前信息目标时提交的公开结算，避免框架猜测完成状态。"""

    target_id: str = Field(min_length=1, max_length=120, pattern=r"^[a-z0-9][a-z0-9._:-]*$")
    status: Literal["complete", "partial", "blocked"]
    summary: str = Field(min_length=1, max_length=1000)
    missing_information: list[str] = Field(default_factory=list, max_length=8)
    # 可引用同一决策中先前已通过语义核验的工具观察，支持纯归纳或比较目标。
    evidence_refs: list[str] = Field(default_factory=list, max_length=12)
    # 区分直接资料结论与基于多条资料形成的保守推断，便于 Trace 公开展示。
    reasoning_basis: Literal["direct_evidence", "conservative_inference"] = "direct_evidence"

    @field_validator("evidence_refs")
    @classmethod
    def evidence_references_are_nonempty_and_unique(cls, values: list[str]) -> list[str]:
        """拒绝空白或重复观察引用，避免结算时产生不可追溯证据。"""
        return list(dict.fromkeys(_validate_identifier_list(values)))


class InformationTarget(ContractModel):
    """专家为当前任务规划的一项可观察信息目标及其执行状态。"""

    target_id: str = Field(min_length=1, max_length=120, pattern=r"^[a-z0-9][a-z0-9._:-]*$")
    objective: str = Field(min_length=1, max_length=300)
    completion_criteria: list[str] = Field(default_factory=list, max_length=6)
    status: Literal["pending", "partial", "complete", "blocked"] = "pending"
    tool_calls_used: int = Field(default=0, ge=0, le=3)
    latest_summary: str | None = Field(default=None, max_length=1000)


class ExpertInformationPlan(ContractModel):
    """专家任务内部的信息目标计划，受五项目标的调用成本上限约束。"""

    targets: list[InformationTarget] = Field(min_length=1, max_length=5)

    @model_validator(mode="after")
    def target_identifiers_are_unique(self) -> "ExpertInformationPlan":
        identifiers = [item.target_id for item in self.targets]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("information target IDs must be unique")
        return self


class ReActDecision(ContractModel):
    """专家模型单轮 ReAct 的受控输出，只允许系统支持的四类下一步动作。"""

    action: Literal["call_tool", "finish", "request_human_input", "request_replan"]
    public_summary: str = Field(min_length=1, max_length=600)
    tool_name: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    hitl_question: str | None = None
    hitl_rationale: str | None = None
    hitl_fields: list[HITLField] = Field(default_factory=list, max_length=3)
    target_resolution: TargetResolution | None = None

    @model_validator(mode="after")
    def action_has_required_payload(self) -> "ReActDecision":
        if self.action == "call_tool" and not self.tool_name:
            raise ValueError("call_tool requires tool_name")
        if self.action == "request_human_input" and (not self.hitl_question or not self.hitl_rationale):
            raise ValueError("request_human_input requires hitl_question and hitl_rationale")
        if self.action == "finish" and self.target_resolution is None:
            raise ValueError("finish requires target_resolution")
        return self


class TargetSettlementSubmission(ContractModel):
    """只结算当前信息目标的完成条件，不跨目标写入覆盖状态。"""

    criteria: list["TargetCriterionSettlement"] = Field(min_length=1, max_length=6)
    coverage_status: Literal["partial", "full"]
    missing_information: list[str] = Field(default_factory=list, max_length=8)
    target_complete: bool
    summary: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def completion_matches_criteria(self) -> "TargetSettlementSubmission":
        """提前结束只能建立在所有当前完成条件均已满足的基础上。"""
        if self.target_complete and not all(item.satisfied for item in self.criteria):
            raise ValueError("complete target settlement requires every criterion to be satisfied")
        if self.target_complete and self.coverage_status != "full":
            raise ValueError("complete target settlement requires full coverage")
        if not self.target_complete and self.coverage_status != "partial":
            raise ValueError("incomplete target settlement requires partial coverage")
        return self


class TargetCriterionSettlement(ContractModel):
    """当前信息目标中一项明确完成条件的公开结算。"""

    criterion: str = Field(min_length=1, max_length=400)
    satisfied: bool
    missing: str | None = Field(default=None, max_length=600)

    @model_validator(mode="after")
    def missing_matches_satisfaction(self) -> "TargetCriterionSettlement":
        if not self.satisfied and not (self.missing or "").strip():
            raise ValueError("unsatisfied criterion requires missing information")
        return self


class ToolBindingAssessment(ContractModel):
    """调用前验证工具参数是否直接服务当前信息目标。"""

    bound: bool
    reason: str = Field(default="", max_length=800)

    @model_validator(mode="after")
    def reason_matches_binding(self) -> "ToolBindingAssessment":
        if not self.bound and not self.reason.strip():
            raise ValueError("unbound assessment requires reason")
        return self


class GeneralTaskResolution(ContractModel):
    """通用 Agent 对不属于专门专家职责的任务给出的单次公开结算。"""

    summary: str = Field(min_length=1, max_length=1000)
    findings: list[str] = Field(default_factory=list, max_length=12)
    uncertainties: list[str] = Field(default_factory=list, max_length=8)
    completion_status: Literal["completed", "completed_with_gaps", "blocked"]


class ProfileSignalExtraction(ContractModel):
    """LLM 从用户亲自输入的文本中抽取的可追溯长期画像或偏好信号。"""

    signals: list[str] = Field(default_factory=list, max_length=12)


class Evidence(ContractModel):
    evidence_id: str = Field(min_length=1)
    decision_id: str = Field(min_length=1)
    claim: str = Field(min_length=1)
    # 表示资料适用的目标、实体、日期或参数范围，供冲突判断排除互补证据。
    scope_key: str | None = Field(default=None, min_length=1, max_length=500)
    value: Any = None
    source: str | None = None
    source_type: str | None = None
    agent: AgentName | None = None
    tool: str | None = None
    confidence: float = Field(default=0.0, ge=0, le=1)
    freshness: float | None = Field(default=None, ge=0, le=1)
    status: EvidenceStatus = EvidenceStatus.PENDING
    supports: list[str] = Field(default_factory=list)
    contradicts: list[str] = Field(default_factory=list)
    retrieved_at: datetime = Field(default_factory=_utc_now)

    @field_validator("supports", "contradicts")
    @classmethod
    def relationship_identifiers_are_nonempty(cls, values: list[str]) -> list[str]:
        return _validate_identifier_list(values)


class EvidenceRelationship(ContractModel):
    """模型对同一证据范围内两条资料的公开语义关系判断。"""

    relation: Literal["supports", "complements", "contradicts", "uncertain"]
    summary: str = Field(min_length=1, max_length=600)


class ToolObservation(ContractModel):
    call_id: str = Field(min_length=1)
    decision_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    # 记录产生本次观察的专家信息目标，避免后续证据与 Trace 丢失归属。
    target_id: str | None = Field(default=None, min_length=1, max_length=120)
    agent: AgentName
    tool_name: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)
    status: ToolCallStatus
    # 传输成功不等于内容对当前目标有用；由 LLM 独立给出语义核验结论。
    semantic_status: ObservationRelevance | None = None
    semantic_summary: str | None = Field(default=None, max_length=1200)
    semantic_missing_information: list[str] = Field(default_factory=list, max_length=8)
    # False 表示资料不直接完成当前目标，但可供同一决策的相关目标在后续结算时引用。
    supports_current_target: bool = True
    coverage_contribution: Literal["partial", "full"] = "partial"
    related_target_ids: list[str] = Field(default_factory=list, max_length=5)
    latency_ms: int | None = Field(default=None, ge=0)
    result_summary: str | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=_utc_now)

    @field_validator("related_target_ids")
    @classmethod
    def related_target_identifiers_are_nonempty_and_unique(cls, values: list[str]) -> list[str]:
        """保证交叉目标资料的归属可审计且不会重复。"""
        return list(dict.fromkeys(_validate_identifier_list(values)))


class TraceSummary(ContractModel):
    """从长工具返回中提炼的公开关键摘要，不包含模型私有推理。"""

    summary: str = Field(min_length=1, max_length=1200)


class ObservationAssessment(ContractModel):
    """LLM 对单次工具返回与当前信息目标的语义相关性判定。"""

    relevance: ObservationRelevance
    summary: str = Field(min_length=1, max_length=1200)
    missing_information: list[str] = Field(default_factory=list, max_length=8)
    # 交叉目标资料仍可为 partial，但不能被当前目标的覆盖更新误用。
    supports_current_target: bool = True
    coverage_contribution: Literal["partial", "full"] = "partial"
    related_target_ids: list[str] = Field(default_factory=list, max_length=5)

    @field_validator("related_target_ids")
    @classmethod
    def related_target_identifiers_are_nonempty_and_unique(cls, values: list[str]) -> list[str]:
        """保持交叉资料的目标 ID 列表可追溯。"""
        return list(dict.fromkeys(_validate_identifier_list(values)))


class AgentResult(ContractModel):
    result_id: str = Field(min_length=1)
    decision_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    agent_name: AgentName
    summary: str = ""
    evidence_ids: list[str] = Field(default_factory=list)
    findings: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    tool_calls_used: int = Field(default=0, ge=0, le=15)
    completion_status: TaskStatus = TaskStatus.COMPLETED
    created_at: datetime = Field(default_factory=_utc_now)

    @field_validator("evidence_ids")
    @classmethod
    def evidence_identifiers_are_nonempty(cls, values: list[str]) -> list[str]:
        return _validate_identifier_list(values)


class ReplanDecision(ContractModel):
    """总控对未完成信息是否值得补救的结构化公开决定。"""

    should_replan: bool
    reason: str = Field(min_length=1, max_length=800)
    critical_gaps: list[str] = Field(default_factory=list, max_length=8)
    can_execute_remedy: bool = False

    @model_validator(mode="after")
    def replan_requires_a_critical_executable_gap(self) -> "ReplanDecision":
        if self.should_replan and (not self.critical_gaps or not self.can_execute_remedy):
            raise ValueError("should_replan requires critical_gaps and can_execute_remedy")
        return self


class DecisionReport(ContractModel):
    recommended_option: str = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    confirmed_facts: list[str] = Field(default_factory=list)
    external_views: list[str] = Field(default_factory=list)
    inferences: list[str] = Field(default_factory=list)
    preference_matches: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    rejected_options: list[str] = Field(default_factory=list)
    tradeoffs: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    next_verification_steps: list[str] = Field(default_factory=list)
    analysis_mode: AnalysisMode = AnalysisMode.MODEL
    fallback_reason: str | None = None


class DecisionRequest(ContractModel):
    query: str = Field(min_length=1)
    decision_type: DecisionType | None = None
    candidates: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    preferences: list[str] = Field(default_factory=list)
    context: dict[str, Any] = Field(default_factory=dict)


class ContinueRequest(ContractModel):
    instruction: str = Field(min_length=1)
    additional_context: dict[str, Any] = Field(default_factory=dict)


class FeedbackRequest(ContractModel):
    user_choice: str | None = None
    outcome: str | None = None
    notes: str | None = None
    chosen_reason: str | None = None
    not_chosen_reason: str | None = None

    @model_validator(mode="after")
    def contains_feedback(self) -> "FeedbackRequest":
        if not any((self.user_choice, self.outcome, self.notes, self.chosen_reason, self.not_chosen_reason)):
            raise ValueError("at least one feedback field is required")
        return self


class FeedbackResponse(ContractModel):
    decision_id: str = Field(min_length=1)
    accepted: bool
    profile_updates: list[str] = Field(default_factory=list)


class RetrospectiveRequest(ContractModel):
    outcome: str | None = Field(default=None, min_length=1)
    notes: str | None = None


class DecisionResponse(ContractModel):
    decision_id: str = Field(min_length=1)
    decision_type: DecisionType
    status: WorkflowStatus
    report: DecisionReport | None = None
    plan: ExecutionPlan | None = None
    events: list["WorkflowEvent"] = Field(default_factory=list)
    activated_agents: list[AgentName] = Field(default_factory=list)
    candidates: list[str] = Field(default_factory=list)


class DecisionListItem(ContractModel):
    decision_id: str = Field(min_length=1)
    decision_type: DecisionType
    query: str = Field(min_length=1)
    status: WorkflowStatus
    recommendation: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)


class ProfileMemory(ContractModel):
    memory_id: str = Field(min_length=1)
    category: str = Field(min_length=1)
    memory_key: str = Field(min_length=1)
    value: Any
    importance: float = Field(ge=0, le=1)
    confidence: float = Field(ge=0, le=1)
    source_episode_ids: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)

    @field_validator("source_episode_ids")
    @classmethod
    def source_episode_identifiers_are_nonempty(cls, values: list[str]) -> list[str]:
        return _validate_identifier_list(values)


class Episode(ContractModel):
    episode_id: str = Field(min_length=1)
    decision_id: str = Field(min_length=1)
    decision_type: DecisionType
    summary: str = Field(min_length=1)
    options: list[str] = Field(default_factory=list)
    recommendation: str | None = None
    user_choice: str | None = None
    key_reasons: list[str] = Field(default_factory=list)
    tradeoffs: list[str] = Field(default_factory=list)
    outcome: str | None = None
    feedback: str | None = None
    chosen_reason: str | None = None
    not_chosen_reason: str | None = None
    constraints: list[str] = Field(default_factory=list)
    preferences: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    profile_signals: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_utc_now)


class MemoryContext(ContractModel):
    profile_memories: list[ProfileMemory] = Field(default_factory=list)
    episodes: list[Episode] = Field(default_factory=list)
    relevant_facts: list[str] = Field(default_factory=list)


class WorkflowEvent(ContractModel):
    """记录决策工作流中可持久化、可向用户展示的一步执行轨迹。"""

    event_id: str = Field(min_length=1)
    decision_id: str = Field(min_length=1)
    from_state: WorkflowStatus | None = None
    to_state: WorkflowStatus
    kind: str = Field(default="workflow_transition", min_length=1)
    title: str = ""
    summary: str = ""
    sequence: int = Field(default=0, ge=0)
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_utc_now)


class ToolDescriptor(ContractModel):
    name: str = Field(min_length=1)
    capability: str = Field(min_length=1)
    description: str = Field(min_length=1)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    allowed_agents: list[AgentName] = Field(default_factory=list)
    read_only: bool = True
