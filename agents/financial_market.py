"""负责金融与市场只读数据分析的专家 Agent。"""

from .base import AgentContext, BaseReActAgent, ToolAction
from models.contracts import AgentName, TaskSpec


class FinancialMarketAgent(BaseReActAgent):
    name = AgentName.FINANCIAL_MARKET

    async def next_action(self, task: TaskSpec, context: AgentContext) -> ToolAction | None:
        """离线降级不臆测证券代码或远程工具参数，直接保留待核验项。"""
        return None
