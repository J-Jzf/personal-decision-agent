"""负责通用外部事实检索与证据整理的专家 Agent。"""

from .base import AgentContext, BaseReActAgent, ToolAction
from models.contracts import AGENT_EXECUTION_CONTRACTS, AgentName, TaskSpec


class EvidenceResearchAgent(BaseReActAgent):
    name = AgentName.EVIDENCE_RESEARCH
    execution_contract = AGENT_EXECUTION_CONTRACTS[name]

    async def next_action(self, task: TaskSpec, context: AgentContext) -> ToolAction | None:
        """模型不可用时不猜测具体工具及其参数，交由本地保守结论标记信息缺口。"""
        return None

    def needs_more(self, task: TaskSpec, context: AgentContext) -> bool:
        return len([o for o in context.observations if o.task_id == task.task_id]) < min(2, len(task.required_capabilities))
