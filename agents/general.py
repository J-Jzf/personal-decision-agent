"""执行一次结构化综合任务的通用 Agent。"""

from uuid import uuid4

from .base import AgentContext, BaseReActAgent
from models.contracts import AgentName, AgentResult, TaskSpec, TaskStatus


class GeneralAgent(BaseReActAgent):
    """用于没有专门执行器的比较、归纳和推荐任务；不调用 MCP。"""

    name = AgentName.GENERAL

    async def execute(self, task: TaskSpec, context: AgentContext) -> AgentResult:
        if task.agent != self.name:
            raise ValueError(f"task {task.task_id} is assigned to {task.agent.value}, not {self.name.value}")
        resolver = getattr(context.model_adapter, "resolve_general_task_or_fallback", None)
        if callable(resolver) and context.request is not None:
            resolution = await resolver(
                task=task, request=context.request, memory=context.memory,
                execution_context=context.execution_context,
            )
            return AgentResult(
                result_id=str(uuid4()), decision_id=context.decision_id, task_id=task.task_id,
                agent_name=self.name, summary=resolution.summary, findings=resolution.findings,
                uncertainties=resolution.uncertainties,
                completion_status=TaskStatus(resolution.completion_status),
            )
        return AgentResult(
            result_id=str(uuid4()), decision_id=context.decision_id, task_id=task.task_id,
            agent_name=self.name, summary="通用综合缺少可用模型，未生成未核验结论。",
            findings=[], uncertainties=["通用综合模型不可用"], completion_status=TaskStatus.BLOCKED,
        )
