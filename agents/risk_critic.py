"""对证据、约束、遗漏与时效性进行对抗性审查的风险专家。"""

from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from .base import AgentContext, BaseReActAgent
from models.contracts import AGENT_EXECUTION_CONTRACTS, AgentName, AgentResult, EvidenceStatus, TaskSpec, TaskStatus


class CriticReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hard_constraint_violations: list[str] = Field(default_factory=list)
    evidence_gaps: list[str] = Field(default_factory=list)
    counterexamples: list[str] = Field(default_factory=list)
    stale_evidence_ids: list[str] = Field(default_factory=list)
    overclaims: list[str] = Field(default_factory=list)
    requires_replan: bool = False


class RiskCritic(BaseReActAgent):
    name = AgentName.RISK_CRITIC
    execution_contract = AGENT_EXECUTION_CONTRACTS[name]

    def review(self, constraints: list[str], context: AgentContext) -> CriticReview:
        evidence = context.evidence_pool.list() if context.evidence_pool is not None else []
        gaps = ["没有外部证据"] if not evidence else []
        gaps.extend(f"未验证证据：{item.evidence_id}" for item in evidence if item.status in {EvidenceStatus.PENDING, EvidenceStatus.UNVERIFIED, EvidenceStatus.UNAVAILABLE})
        conflicts = [f"证据冲突：{item.evidence_id}" for item in evidence if item.status == EvidenceStatus.CONFLICTING]
        return CriticReview(
            hard_constraint_violations=[item for item in constraints if any(term in item for term in ("不满足", "超过", "违规"))],
            evidence_gaps=gaps, counterexamples=conflicts,
            stale_evidence_ids=[item.evidence_id for item in evidence if item.freshness is not None and item.freshness < .4],
            overclaims=["不得把 Agent 推断标注为已确认事实"] if gaps else [],
            requires_replan=bool(gaps or conflicts),
        )

    async def execute(self, task: TaskSpec, context: AgentContext) -> AgentResult:
        review = self.review(list(context.request_context.get("constraints", [])), context)
        findings = [*review.hard_constraint_violations, *review.evidence_gaps, *review.counterexamples, *review.overclaims]
        return AgentResult(result_id=str(uuid4()), decision_id=context.decision_id, task_id=task.task_id,
            agent_name=self.name, summary="已完成对抗性风险检查", findings=findings,
            uncertainties=review.evidence_gaps, completion_status=TaskStatus.COMPLETED)
