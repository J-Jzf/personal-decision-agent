"""由 Skill 驱动的 Plan-and-Execute 任务 DAG 构建器。"""

from __future__ import annotations

from models.contracts import AGENT_EXECUTION_CONTRACTS, AgentName, AutonomousPlan, DecisionRequest, DecisionType, ExecutionPlan, MemoryContext, TaskSpec, TaskWorkKind
from skills.registry import SkillDefinition


AGENT_EXECUTION_CATALOG = AGENT_EXECUTION_CONTRACTS


class Planner:
    """将模型自主规划转换为受工具目录和专家权限约束的可执行任务图。"""

    def __init__(self, model_adapter=None) -> None:
        self.model_adapter = model_adapter

    async def create_autonomous_plan(self, request: DecisionRequest, *, skills: list[SkillDefinition],
                                     tools: list[object], memory: MemoryContext,
                                     execution_context: dict[str, object] | None = None) -> AutonomousPlan:
        """优先让总控模型选 Skill、专家与 DAG；无模型时才返回保守本地计划。"""
        catalog = [definition.to_dict() | {"body": definition.body} for definition in skills]
        if self.model_adapter is None:
            fallback = self.create_plan(request, next((item for item in skills if item.name == "risk-debate-moderator"), skills[0]), request.decision_type or DecisionType.GENERAL)
            return AutonomousPlan(decision_type=request.decision_type or DecisionType.GENERAL, skill_name="risk-debate-moderator", planning_summary="未配置模型，使用本地保守计划。", plan=await fallback)
        selected = await self.model_adapter.autonomous_plan_or_fallback(
            request, skills=catalog, tools=tools, memory=memory,
            execution_context=execution_context or {},
        )
        valid_skills = {definition.name for definition in skills}
        skill_name = selected.skill_name if selected.skill_name in valid_skills else None
        allowed_agents = set(AGENT_EXECUTION_CATALOG)
        available_capabilities = {
            descriptor.capability for descriptor in tools
            if hasattr(descriptor, "capability")
        }
        tasks: list[TaskSpec] = []
        for task in selected.plan.tasks:
            if task.agent not in allowed_agents:
                continue
            agent = self.route_agent(task)
            permitted = [
                capability for capability in task.required_capabilities
                if capability in available_capabilities and capability in self._capabilities_for_agent(agent)
            ]
            tasks.append(task.model_copy(update={"agent": agent, "required_capabilities": permitted}))
        plan = selected.plan.model_copy(update={"tasks": tasks})
        return selected.model_copy(update={"skill_name": skill_name, "plan": plan})

    async def create_plan(self, request: DecisionRequest, skill: SkillDefinition, decision_type: DecisionType) -> ExecutionPlan:
        agents = self._agents_for(request, skill, decision_type)
        tasks: list[TaskSpec] = []
        task_ids: list[str] = []
        for agent in agents:
            identifier = agent.value
            dependencies = list(task_ids) if agent in {AgentName.GENERAL, AgentName.RISK_CRITIC} else []
            capabilities = self._capabilities(agent, skill)
            tasks.append(TaskSpec(
                task_id=identifier, objective=self._objective(agent, request, skill), agent=agent,
                dependencies=dependencies, required_capabilities=capabilities,
                completion_criteria=skill.completion_conditions[:3], work_kind=self._work_kind(agent),
            ))
            task_ids.append(identifier)
        requires_debate = "risk-debate-moderator" == skill.name or any(term in request.query for term in ("争议", "冲突", "高风险"))
        return ExecutionPlan(
            goal=request.query, tasks=tasks,
            missing_information=[],
            requires_verification="evidence_research" in {agent.value for agent in agents},
            requires_debate=requires_debate,
            replan_conditions=["关键资料持续不可用", "重要证据冲突", "硬约束违规", "Critic 提出关键遗漏"],
        )

    def _agents_for(self, request: DecisionRequest, skill: SkillDefinition, decision_type: DecisionType) -> list[AgentName]:
        base = [AgentName.EVIDENCE_RESEARCH, AgentName.PREFERENCE]
        if decision_type == DecisionType.PORTFOLIO or any(term in request.query.casefold() for term in ("股票", "etf", "rsu", "金融资产")):
            base.append(AgentName.FINANCIAL_MARKET)
        if decision_type == DecisionType.TRAVEL or any(term in request.query.casefold() for term in ("跨城", "搬家", "通勤", "上海", "杭州", "厦门", "地点")):
            base.append(AgentName.LOCATION_LIFESTYLE)
        for raw in skill.recommended_agents:
            try:
                agent = AgentName(raw)
            except ValueError:
                continue
            if agent in AGENT_EXECUTION_CATALOG:
                base.append(agent)
        base.append(AgentName.GENERAL)
        base.append(AgentName.RISK_CRITIC)
        return list(dict.fromkeys(base))

    @staticmethod
    def _capabilities(agent: AgentName, skill: SkillDefinition) -> list[str]:
        allowed = Planner._capabilities_for_agent(agent)
        return [tool for tool in skill.recommended_tools if tool in allowed] or ([sorted(allowed)[0]] if allowed else [])

    @staticmethod
    def _capabilities_for_agent(agent: AgentName) -> set[str]:
        contract = AGENT_EXECUTION_CATALOG.get(agent)
        return set(contract.capabilities) if contract else set()

    @staticmethod
    def agent_can_execute(agent: AgentName, work_kind: TaskWorkKind,
                          required_capabilities: list[str] | None = None) -> bool:
        """只以结构化 execute 合同校验路由，不从 objective 文案猜测职责。"""
        contract = AGENT_EXECUTION_CATALOG.get(agent)
        return bool(contract and work_kind in contract.work_kinds and set(required_capabilities or []).issubset(set(contract.capabilities)))

    @classmethod
    def route_agent(cls, task: TaskSpec) -> AgentName:
        if cls.agent_can_execute(task.agent, task.work_kind, task.required_capabilities):
            return task.agent
        for agent in AGENT_EXECUTION_CATALOG:
            if cls.agent_can_execute(agent, task.work_kind, task.required_capabilities):
                return agent
        return AgentName.GENERAL

    @staticmethod
    def _objective(agent: AgentName, request: DecisionRequest, skill: SkillDefinition) -> str:
        if agent is AgentName.PREFERENCE:
            return "读取并匹配用户显式偏好和历史记忆；只输出可追溯的偏好信号，不做综合比较或推荐。"
        labels = {
            AgentName.EVIDENCE_RESEARCH: "检索并标注外部证据",
            AgentName.FINANCIAL_MARKET: "分析市场与财务数据",
            AgentName.LOCATION_LIFESTYLE: "比较地点、通勤、天气与生活方式",
            AgentName.RISK_CRITIC: "对硬约束、证据质量、遗漏和反例做对抗检查",
            AgentName.GENERAL: "综合已有资料、用户偏好与约束，形成可追溯的比较或推荐",
        }
        return f"{labels[agent]}：{request.query}；维度：{', '.join(skill.analysis_dimensions)}"

    @staticmethod
    def _work_kind(agent: AgentName) -> TaskWorkKind:
        return AGENT_EXECUTION_CATALOG[agent].work_kinds[0]
