"""执行一次结构化综合任务的通用 Agent。"""

from uuid import uuid4

from .base import AgentContext, BaseReActAgent
from models.contracts import AGENT_EXECUTION_CONTRACTS, AgentName, AgentResult, TaskSpec, TaskStatus


class GeneralAgent(BaseReActAgent):
    """用于没有专门执行器的比较、归纳和推荐任务；不调用 MCP。"""

    name = AgentName.GENERAL
    execution_contract = AGENT_EXECUTION_CONTRACTS[name]

    async def execute(self, task: TaskSpec, context: AgentContext) -> AgentResult:
        if task.agent != self.name:
            raise ValueError(f"task {task.task_id} is assigned to {task.agent.value}, not {self.name.value}")
        resolver = getattr(context.model_adapter, "resolve_general_task_or_fallback", None)
        if callable(resolver) and context.request is not None:
            delegation_planner = getattr(context.model_adapter, "plan_general_delegations_or_fallback", None)
            if task.allow_factual_delegation and callable(delegation_planner) and callable(context.specialist_delegate):
                plan = await delegation_planner(
                    task=task, request=context.request, memory=context.memory,
                    execution_context=context.execution_context,
                )
                for request in plan.delegations:
                    delegated = await context.specialist_delegate(task, request, context)
                    context.delegated_results.append(delegated)
            resolution_context = {
                **context.execution_context,
                "delegated_results": list(context.delegated_results),
            }
            resolution = await resolver(
                task=task, request=context.request, memory=context.memory,
                execution_context=resolution_context,
            )
            delegated_findings = [
                finding for result in context.delegated_results
                for finding in result.get("findings", []) if isinstance(finding, str)
            ]
            return AgentResult(
                result_id=str(uuid4()), decision_id=context.decision_id, task_id=task.task_id,
                agent_name=self.name, summary=resolution.summary,
                findings=list(dict.fromkeys([*delegated_findings, *resolution.findings])),
                uncertainties=resolution.uncertainties,
                completion_status=TaskStatus(resolution.completion_status),
            )
        return AgentResult(
            result_id=str(uuid4()), decision_id=context.decision_id, task_id=task.task_id,
            agent_name=self.name, summary="通用综合缺少可用模型，未生成未核验结论。",
            findings=[], uncertainties=["通用综合模型不可用"], completion_status=TaskStatus.BLOCKED,
        )
