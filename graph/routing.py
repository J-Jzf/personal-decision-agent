"""独立保存、便于检查与测试的工作流条件路由规则。"""

from __future__ import annotations

from typing import Any

from models.contracts import ReplanDecision


async def route_after_execution(state: Any, evidence_pool: Any, model_adapter: Any | None = None) -> str:
    """仅在总控认定缺口关键且可补救时重规划，普通不确定性直接留给后续阶段。"""
    current_task_ids = {task.task_id for task in (state.plan.tasks if state.plan else [])}
    latest_results = {}
    for result in reversed(state.agent_results):
        latest_results.setdefault(result.task_id, result)
    unresolved = [
        result for task_id, result in latest_results.items()
        if task_id in current_task_ids and result.uncertainties
    ]
    state.replan_decision = None
    if unresolved:
        context = {
            "current_plan": state.plan.model_dump(mode="json") if state.plan else {},
            "task_ledger": state.task_ledger,
            "unresolved_results": [item.model_dump(mode="json") for item in unresolved],
            "information_targets": state.information_targets,
            "information_coverage": state.information_coverage,
            "tool_observations": [item.model_dump(mode="json") for item in state.tool_observations],
            "evidence": [item.model_dump(mode="json") for item in evidence_pool.list()],
        }
        if model_adapter is not None:
            decision = await model_adapter.replan_decision_or_fallback(
                request=state.request, execution_context=context,
            )
        else:
            decision = ReplanDecision(
                should_replan=False,
                reason="没有可用总控模型判断补救价值，保留缺口并继续后续判断。",
                critical_gaps=[], can_execute_remedy=False,
            )
        state.replan_decision = decision
        if decision.should_replan:
            return "replan"
    if evidence_pool.conflicts() or (state.plan and state.plan.requires_verification):
        return "verify"
    if state.plan and state.plan.requires_debate:
        return "debate"
    return "judge"


def should_debate(state: Any, evidence_pool: Any) -> bool:
    """保持辩论阶段的既有入口，仅由计划显式要求或证据池冲突触发。"""
    return bool((state.plan and state.plan.requires_debate) or evidence_pool.conflicts())
