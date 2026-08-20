"""只能读取传入记忆上下文的个人偏好分析专家。"""

from uuid import uuid4

from .base import AgentContext, BaseReActAgent
from models.contracts import AGENT_EXECUTION_CONTRACTS, AgentName, AgentResult, TaskSpec, TaskStatus


class PreferenceAgent(BaseReActAgent):
    name = AgentName.PREFERENCE
    execution_contract = AGENT_EXECUTION_CONTRACTS[name]

    async def execute(self, task: TaskSpec, context: AgentContext) -> AgentResult:
        findings = [f"{item.memory_key}={item.value}（置信度 {item.confidence:.2f}）" for item in context.memory.profile_memories]
        return AgentResult(result_id=str(uuid4()), decision_id=context.decision_id, task_id=task.task_id,
            agent_name=self.name, summary="已读取任务范围内的个人记忆", findings=findings,
            uncertainties=[] if findings else ["没有稳定的长期偏好记忆"], completion_status=TaskStatus.COMPLETED)
