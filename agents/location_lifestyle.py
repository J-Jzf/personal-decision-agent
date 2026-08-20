"""负责地点、路线、天气和生活方式因素分析的专家 Agent。"""

from .base import AgentContext, BaseReActAgent, ToolAction
from models.contracts import AGENT_EXECUTION_CONTRACTS, AgentName, TaskSpec


class LocationLifestyleAgent(BaseReActAgent):
    name = AgentName.LOCATION_LIFESTYLE
    execution_contract = AGENT_EXECUTION_CONTRACTS[name]

    async def next_action(self, task: TaskSpec, context: AgentContext) -> ToolAction | None:
        """离线降级不把地点名称误填进天气、路线或抓取工具的不同参数合同。"""
        return None

    def needs_more(self, task: TaskSpec, context: AgentContext) -> bool:
        return len([item for item in context.observations if item.task_id == task.task_id]) < min(2, len(task.required_capabilities))
