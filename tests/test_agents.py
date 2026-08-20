import asyncio

from agents.base import AgentContext, BaseReActAgent, ToolAction
from agents.planner import Planner
from models.contracts import AgentName, DecisionRequest, DecisionType, ExpertInformationPlan, InformationCoverageUpdate, InformationTarget, MemoryContext, ObservationAssessment, ObservationRelevance, TargetResolution, TaskStatus, ToolCallStatus, ToolObservation
from skills.registry import SkillRegistry


def test_react_context_is_limited_to_the_active_target_and_its_history():
    """ReAct 不得因其他 target 的状态、资料或缺口受到污染。"""
    from models.contracts import TaskSpec

    task = TaskSpec(
        task_id="weather", objective="比较南京和苏州的周末天气", agent=AgentName.LOCATION_LIFESTYLE,
        completion_criteria=["获得南京与苏州的天气"],
    )
    observations = [
        {"tool_name": "weather", "arguments": {"location": "南京"}, "status": "failed", "error": "temporary error"},
        {"tool_name": "weather", "arguments": {"location": "苏州"}, "status": "succeeded", "result_summary": "苏州天气已获得"},
    ]
    context = AgentContext(
        decision_id="d", memory=MemoryContext(), request=DecisionRequest(query="南京和苏州怎么选"),
        execution_context={
            "all_tasks": [{"task_id": "weather"}],
            "structured_coverage": {"weather": {"covered_locations": ["南京"], "missing_locations": ["苏州"]}},
            "all_successful_information": [{"task_id": "earlier", "result_summary": "南京天气已获得"}],
            "active_information_target": {
                "target_id": "suzhou", "objective": "获取苏州天气", "completion_criteria": ["苏州天气"],
                "status": "partial", "latest_summary": "苏州温度已取得", "missing_information": ["苏州降雨"],
            },
        },
        information_targets=[
            {"target_id": "nanjing", "objective": "获取南京天气", "status": "complete", "latest_summary": "南京已完成"},
            {"target_id": "suzhou", "objective": "获取苏州天气", "status": "partial", "latest_summary": "苏州温度已取得"},
        ],
    )

    prompt_history = BaseReActAgent._prompt_task_history(observations)
    view = BaseReActAgent._react_context_view(task, context, prompt_history, {})

    assert view["当前 target"]["target_id"] == "suzhou"
    assert view["当前 target completion_criteria"] == ["苏州天气"]
    assert view["当前 target 已有 observations"][0]["status"] == "failed"
    assert view["当前 target latest_summary"] == "苏州温度已取得"
    assert view["当前 target missing_information"] == ["苏州降雨"]
    assert "所有 target" not in view
    assert "所有任务" not in view
    assert "南京已完成" not in str(view)
    assert "南京天气已获得" not in str(view)


def test_information_coverage_replaces_partial_result_with_a_complete_result():
    """同一信息目标取得完整资料后，应保留部分资料历史并更新当前有效状态。"""
    context = AgentContext(decision_id="d", memory=MemoryContext())

    context.apply_coverage_updates([
        InformationCoverageUpdate(
            target_key="weekend-comparison", target="比较两个选项的周末条件",
            status="partial", summary="已取得第一个选项的资料。",
        )
    ])
    context.apply_coverage_updates([
        InformationCoverageUpdate(
            target_key="weekend-comparison", target="比较两个选项的周末条件",
            status="complete", summary="已取得两个选项的完整可比资料。",
        )
    ])

    record = context.information_coverage["weekend-comparison"]
    assert record["status"] == "complete"
    assert record["latest_summary"] == "已取得两个选项的完整可比资料。"
    assert record["history"][0]["status"] == "superseded"
    assert record["history"][0]["previous_status"] == "partial"


def test_react_context_hides_cross_target_evidence_from_the_active_target():
    """旁路资料只交给总控，当前 target 的 ReAct 不得直接引用它。"""
    from models.contracts import TaskSpec

    context = AgentContext(
        decision_id="d", memory=MemoryContext(), request=DecisionRequest(query="比较 A 和 B"),
        execution_context={
            "evidence_ledger": [{
                "call_id": "call-a", "decision_id": "d", "task_id": "source",
                "status": "succeeded", "semantic_status": "relevant", "result_summary": "A 的已验证资料",
            }],
        },
    )

    view = BaseReActAgent._react_context_view(
        TaskSpec(task_id="comparison", objective="比较 A 和 B", agent=AgentName.EVIDENCE_RESEARCH),
        context, [], {},
    )

    assert "可引用证据账本" not in view
    assert "call-a" not in str(view)


def test_result_for_another_known_target_is_referenceable_without_completing_the_current_target():
    """同一决策中的交叉资料应保留给后续目标，但不能误算为当前目标已经完成。"""
    observation = ToolObservation(
        call_id="call-b", decision_id="d", task_id="research", target_id="target-a",
        agent=AgentName.EVIDENCE_RESEARCH, tool_name="search", status=ToolCallStatus.SUCCEEDED,
        semantic_status=ObservationRelevance.PARTIAL, supports_current_target=False,
        related_target_ids=["target-b"], result_summary="选项 B 的可用资料。",
    )

    assert not BaseReActAgent._is_semantically_usable(observation)
    assert BaseReActAgent._is_referenceable_observation(observation)


def test_expert_information_plan_limits_targets_to_five():
    """专家内部计划最多包含五个可审计信息目标。"""
    plan = ExpertInformationPlan(targets=[
        InformationTarget(target_id=f"target-{index}", objective=f"补齐资料 {index}", completion_criteria=["得到可比资料"])
        for index in range(5)
    ])
    assert len(plan.targets) == 5


class LoopingAgent(BaseReActAgent):
    name = AgentName.EVIDENCE_RESEARCH
    async def next_action(self, task, context):
        return ToolAction(tool_name=f"web_search_{len(context.observations)}", arguments={"query": task.objective})


class Gateway:
    async def call_tool(self, agent, tool_name, arguments, **kwargs):
        return ToolObservation(call_id=str(len(arguments) + len(tool_name)), decision_id=kwargs["decision_id"], task_id=kwargs["task_id"], agent=agent, tool_name=tool_name, status=ToolCallStatus.UNAVAILABLE)


class RepeatingAgent(BaseReActAgent):
    name = AgentName.EVIDENCE_RESEARCH

    async def next_action(self, task, context):
        return ToolAction(tool_name="brave_web_search", arguments={"query": task.objective})


class CountingGateway(Gateway):
    def __init__(self):
        self.calls = 0

    async def call_tool(self, agent, tool_name, arguments, **kwargs):
        self.calls += 1
        return await super().call_tool(agent, tool_name, arguments, **kwargs)


class SuccessfulGateway(CountingGateway):
    async def call_tool(self, agent, tool_name, arguments, **kwargs):
        self.calls += 1
        return ToolObservation(
            call_id=str(self.calls), decision_id=kwargs["decision_id"], task_id=kwargs["task_id"],
            agent=agent, tool_name=tool_name, status=ToolCallStatus.SUCCEEDED, result_summary="已取得结果",
        )


class SuccessfulRepeatingAgent(RepeatingAgent):
    def needs_more(self, task, context):
        return True


class SemanticallySuccessfulRepeatingAgent(SuccessfulRepeatingAgent):
    async def _assess_observation(self, task, context, target, observation):
        """为任务级状态测试提供已经通过语义核验的工具资料。"""
        return observation.model_copy(update={"semantic_status": ObservationRelevance.RELEVANT, "semantic_summary": "资料可用。"})


def test_cross_city_offer_routes_location_and_limits_react_to_three():
    registry = SkillRegistry(__import__("pathlib").Path("skills")); registry.load_all()
    request = DecisionRequest(query="上海工作与杭州 AI Offer 怎么选")
    plan = asyncio.run(Planner().create_plan(request, registry.get("job-offer-evaluator"), DecisionType.JOB_OFFER))
    assigned = {task.agent.value for task in plan.tasks}
    assert {"evidence_research", "preference", "location_lifestyle", "risk_critic"} <= assigned
    context = AgentContext(decision_id="d", gateway=Gateway(), memory=MemoryContext())
    result = asyncio.run(LoopingAgent().execute(plan.tasks[0], context))
    assert result.tool_calls_used == 3


def test_portfolio_routes_financial_agent():
    registry = SkillRegistry(__import__("pathlib").Path("skills")); registry.load_all()
    request = DecisionRequest(query="我的 ETF 和股票是否过度集中")
    plan = asyncio.run(Planner().create_plan(request, registry.get("portfolio-review"), DecisionType.PORTFOLIO))
    assert AgentName.FINANCIAL_MARKET in {task.agent for task in plan.tasks}


def test_planner_routes_synthesis_to_general_instead_of_preference_memory_reader():
    """PreferenceAgent 只读记忆，综合推荐必须交给实际能执行综合的 GeneralAgent。"""
    registry = SkillRegistry(__import__("pathlib").Path("skills")); registry.load_all()
    plan = asyncio.run(Planner().create_plan(
        DecisionRequest(query="综合天气、景点、偏好和预算，推荐南京或苏州"),
        registry.get("travel-destination-compare"), DecisionType.TRAVEL,
    ))

    general_task = next(task for task in plan.tasks if task.agent is AgentName.GENERAL)
    preference_task = next(task for task in plan.tasks if task.agent is AgentName.PREFERENCE)
    assert "综合" in general_task.objective
    assert "用户显式偏好" in preference_task.objective
    assert "南京" not in preference_task.objective
    assert not Planner.agent_can_execute(AgentName.PREFERENCE, "综合天气、景点和预算后推荐城市")


def test_react_blocks_duplicate_tool_name_and_arguments_before_a_second_mcp_call():
    """重复的同工具同参数调用应作为失败观察反馈给专家，而非再次访问外部服务。"""
    task = __import__("models.contracts", fromlist=["TaskSpec"]).TaskSpec(
        task_id="research", objective="上海周末天气", agent=AgentName.EVIDENCE_RESEARCH
    )
    gateway = CountingGateway()
    context = AgentContext(decision_id="d", gateway=gateway, memory=MemoryContext())

    result = asyncio.run(RepeatingAgent().execute(task, context))

    assert gateway.calls == 1
    assert result.tool_calls_used == 1
    assert any("重复工具调用已阻止" in (item.error or "") for item in context.observations)


def test_react_allows_rechecking_a_previously_successful_tool_call():
    """相同工具和参数若上次成功，专家可以在确有需要时再次核验。"""
    task = __import__("models.contracts", fromlist=["TaskSpec"]).TaskSpec(
        task_id="research", objective="上海周末天气", agent=AgentName.EVIDENCE_RESEARCH
    )
    gateway = SuccessfulGateway()
    context = AgentContext(decision_id="d", gateway=gateway, memory=MemoryContext())

    result = asyncio.run(SuccessfulRepeatingAgent().execute(task, context))

    assert gateway.calls == 3
    assert result.tool_calls_used == 3
    assert not any("重复工具调用已阻止" in (item.error or "") for item in context.observations)


def test_react_marks_task_completed_with_gaps_when_the_tool_call_budget_is_exhausted_after_useful_results():
    """额度耗尽但已有可用资料时，应交给下游保守综合而非把整个任务标为 blocked。"""
    task = __import__("models.contracts", fromlist=["TaskSpec"]).TaskSpec(
        task_id="research", objective="上海周末天气", agent=AgentName.EVIDENCE_RESEARCH,
        required_capabilities=["web_search"], completion_criteria=["得到两地天气"],
    )
    result = asyncio.run(SemanticallySuccessfulRepeatingAgent().execute(
        task, AgentContext(decision_id="d", gateway=SuccessfulGateway(), memory=MemoryContext())
    ))

    assert result.tool_calls_used == 3
    assert result.completion_status is TaskStatus.COMPLETED_WITH_GAPS
    assert any("调用额度耗尽" in item for item in result.uncertainties)


def test_react_switches_target_immediately_after_a_complete_coverage_update():
    """工具观察的专用结算节点完成目标后，专家不得再承接下一次调用。"""
    from models.contracts import ReActDecision, TaskSpec

    class PlannedAdapter:
        def __init__(self):
            self.decisions = 0

        async def information_plan_or_fallback(self, **kwargs):
            return ExpertInformationPlan(targets=[
                InformationTarget(target_id="nanjing", objective="南京资料", completion_criteria=["已获得南京资料"]),
                InformationTarget(target_id="suzhou", objective="苏州资料", completion_criteria=["已获得苏州资料"]),
            ])

        async def react_or_fallback(self, **kwargs):
            self.decisions += 1
            if self.decisions == 1:
                return ReActDecision(action="call_tool", public_summary="查询南京", tool_name="search", arguments={"q": "南京"})
            assert kwargs["execution_context"]["active_information_target"]["target_id"] == "suzhou"
            return ReActDecision(action="request_replan", public_summary="停止后续测试调用。")

        async def assess_observation_or_fallback(self, **kwargs):
            return ObservationAssessment(relevance="relevant", summary="结果支持当前城市资料。")

        async def settle_current_target_after_observation_or_none(self, **kwargs):
            from models.contracts import TargetCriterionSettlement, TargetSettlementSubmission
            assert kwargs["current_target"]["target_id"] == "nanjing"
            return TargetSettlementSubmission(
                criteria=[TargetCriterionSettlement(criterion="已获得南京资料", satisfied=True)],
                coverage_status="full", missing_information=[], target_complete=True, summary="南京资料已获得",
            )

    class TargetGateway:
        def __init__(self):
            self.target_ids: list[str] = []

        async def call_tool(self, agent, tool_name, arguments, **kwargs):
            self.target_ids.append(kwargs["target_id"])
            return ToolObservation(
                call_id=str(len(self.target_ids)), decision_id=kwargs["decision_id"], task_id=kwargs["task_id"],
                agent=agent, tool_name=tool_name, arguments=arguments, status=ToolCallStatus.SUCCEEDED, result_summary="已取得结果",
            )

    gateway = TargetGateway()
    context = AgentContext(
        decision_id="d", gateway=gateway, memory=MemoryContext(), model_adapter=PlannedAdapter(),
        request=DecisionRequest(query="比较南京和苏州"),
    )
    asyncio.run(BaseReActAgent().execute(
        TaskSpec(task_id="research", objective="比较城市", agent=AgentName.EVIDENCE_RESEARCH), context,
    ))

    assert gateway.target_ids == ["nanjing"]
    assert context.information_targets[0]["status"] == "complete"


def test_react_settles_a_usable_observation_before_requesting_the_next_react_action():
    """语义可用观察产生后，应立即进入专用结算节点而不是等待下一轮 ReAct。"""
    from models.contracts import ReActDecision, TaskSpec

    class ImmediateSettlementAdapter:
        def __init__(self):
            self.react_calls = 0
            self.settlement_requests = 0

        async def information_plan_or_fallback(self, **kwargs):
            return ExpertInformationPlan(targets=[InformationTarget(
                target_id="city-weather", objective="获取城市周末天气", completion_criteria=["得到天气参考"],
            )])

        async def react_or_fallback(self, **kwargs):
            self.react_calls += 1
            if self.react_calls == 1:
                return ReActDecision(
                    action="call_tool", public_summary="查询天气", tool_name="weather", arguments={"location": "甲地"},
                )
            raise AssertionError("结算完成后不应再为同一 complete 目标调用 ReAct。")

        async def assess_observation_or_fallback(self, **kwargs):
            return ObservationAssessment(relevance="relevant", summary="天气资料支持当前目标。")

        async def settle_current_target_after_observation_or_none(self, **kwargs):
            from models.contracts import TargetCriterionSettlement, TargetSettlementSubmission
            self.settlement_requests += 1
            assert kwargs["current_target"]["target_id"] == "city-weather"
            assert kwargs["current_target"]["completion_criteria"] == ["得到天气参考"]
            assert kwargs["target_observations"][0]["tool_name"] == "weather"
            assert kwargs["target_observations"][0]["semantic_status"] == "relevant"
            return TargetSettlementSubmission(
                criteria=[TargetCriterionSettlement(criterion="得到天气参考", satisfied=True)],
                coverage_status="full", missing_information=[], target_complete=True, summary="天气资料已经足够参考。",
            )

    class SingleWeatherGateway:
        def __init__(self):
            self.calls = 0

        async def call_tool(self, agent, tool_name, arguments, **kwargs):
            self.calls += 1
            return ToolObservation(
                call_id="weather-call", decision_id=kwargs["decision_id"], task_id=kwargs["task_id"],
                agent=agent, tool_name=tool_name, arguments=arguments,
                status=ToolCallStatus.SUCCEEDED, result_summary="甲地本周末天气适合出行。",
            )

    adapter = ImmediateSettlementAdapter()
    gateway = SingleWeatherGateway()
    context = AgentContext(
        decision_id="d", gateway=gateway, memory=MemoryContext(), model_adapter=adapter,
        request=DecisionRequest(query="甲地周末天气如何？"),
    )

    asyncio.run(BaseReActAgent().execute(
        TaskSpec(task_id="weather", objective="获取天气", agent=AgentName.EVIDENCE_RESEARCH), context,
    ))

    assert adapter.settlement_requests == 1
    assert adapter.react_calls == 1
    assert gateway.calls == 1
    assert context.information_targets[0]["status"] == "complete"


def test_semantically_irrelevant_transport_success_does_not_become_an_agent_finding():
    """MCP 返回成功但内容属于其他城市时，不能成为当前任务的成功发现。"""
    from models.contracts import ReActDecision, TaskSpec

    class SemanticAdapter:
        def __init__(self):
            self.turn = 0

        async def information_plan_or_fallback(self, **kwargs):
            return ExpertInformationPlan(targets=[InformationTarget(
                target_id="nanjing-spots", objective="获取南京安静景点", completion_criteria=["得到南京景点"],
            )])

        async def react_or_fallback(self, **kwargs):
            self.turn += 1
            if self.turn == 1:
                return ReActDecision(action="call_tool", public_summary="查询景点", tool_name="search", arguments={"query": "南京安静景点"})
            return ReActDecision(
                action="finish", public_summary="无可用南京资料，保留缺口。",
                target_resolution=TargetResolution(
                    target_id="nanjing-spots", status="blocked", summary="返回的是北京景点，不能支撑南京目标。",
                    missing_information=["南京安静景点"],
                ),
            )

        async def assess_observation_or_fallback(self, **kwargs):
            return ObservationAssessment(
                relevance="irrelevant", summary="结果是北京景点，与南京目标无关。", missing_information=["南京安静景点"],
            )

    class WrongCityGateway:
        async def call_tool(self, agent, tool_name, arguments, **kwargs):
            return ToolObservation(
                call_id="c", decision_id=kwargs["decision_id"], task_id=kwargs["task_id"], agent=agent,
                tool_name=tool_name, arguments=arguments, status=ToolCallStatus.SUCCEEDED,
                result_summary="北京丫髻山景点介绍。",
            )

    context = AgentContext(
        decision_id="d", gateway=WrongCityGateway(), memory=MemoryContext(), model_adapter=SemanticAdapter(),
        request=DecisionRequest(query="比较南京与苏州的安静景点"),
    )
    result = asyncio.run(BaseReActAgent().execute(
        TaskSpec(task_id="attractions", objective="比较景点", agent=AgentName.EVIDENCE_RESEARCH, required_capabilities=["web_search"]),
        context,
    ))

    assert result.findings == []
    assert context.observations[0].semantic_status == "irrelevant"


def test_react_rejects_a_derived_completion_that_references_unknown_evidence():
    """模型不得用不存在的观察 ID 把纯归纳目标伪装为已完成。"""
    from models.contracts import ReActDecision, TaskSpec

    target = {"target_id": "comparison", "objective": "比较资料", "completion_criteria": ["完成比较"]}
    decision = ReActDecision(
        action="finish", public_summary="完成比较", target_resolution=TargetResolution(
            target_id="comparison", status="complete", summary="引用不存在资料。",
            evidence_refs=["missing-call"], reasoning_basis="conservative_inference",
        ),
    )
    agent = BaseReActAgent()
    context = AgentContext(decision_id="d", memory=MemoryContext())

    error = asyncio.run(agent._apply_target_resolution(
        TaskSpec(task_id="research", objective="比较资料", agent=AgentName.EVIDENCE_RESEARCH),
        context, target, decision, False,
    ))

    assert error is not None
    assert "evidence_refs" in error


def test_unbound_preflight_rejects_parameters_before_mcp_without_spending_tool_quota():
    """当前苏州目标不能因查询南京而浪费任何一次 MCP 调用。"""
    from models.contracts import ReActDecision, TaskSpec, ToolBindingAssessment

    class PreflightAdapter:
        async def information_plan_or_fallback(self, **kwargs):
            return ExpertInformationPlan(targets=[InformationTarget(
                target_id="suzhou-weather", objective="获取苏州周末天气", completion_criteria=["苏州天气"],
            )])

        async def react_or_fallback(self, **kwargs):
            if kwargs["execution_context"].get("react_validation_error"):
                return ReActDecision(action="request_replan", public_summary="参数未绑定，停止当前测试。")
            return ReActDecision(
                action="call_tool", public_summary="查询天气", tool_name="weather", arguments={"city": "Nanjing"},
            )

        async def assess_tool_binding_or_fallback(self, **kwargs):
            assert kwargs["target"]["target_id"] == "suzhou-weather"
            assert kwargs["arguments"] == {"city": "Nanjing"}
            assert "information_targets" not in kwargs
            return ToolBindingAssessment(bound=False, reason="参数查询南京，不服务苏州天气目标。")

    class NeverCalledGateway:
        def __init__(self):
            self.calls = 0

        async def call_tool(self, *args, **kwargs):
            self.calls += 1
            raise AssertionError("未绑定参数不得调用 MCP")

    gateway = NeverCalledGateway()
    context = AgentContext(
        decision_id="d", gateway=gateway, memory=MemoryContext(), model_adapter=PreflightAdapter(),
        request=DecisionRequest(query="南京和苏州周末旅游怎么选"),
        available_tools=[{"name": "weather", "input_schema": {"type": "object"}}],
    )
    asyncio.run(BaseReActAgent().execute(
        TaskSpec(task_id="weather", objective="比较天气", agent=AgentName.EVIDENCE_RESEARCH), context,
    ))

    target = context.information_targets[0]
    assert gateway.calls == 0
    assert target["binding_calls_used"] == 1
    assert target["tool_calls_used"] == 0
    assert any("不服务苏州天气" in (item.error or "") for item in context.observations)


def test_partial_settlement_keeps_the_same_target_active_until_a_full_settlement():
    """partial 只能累计当前目标资料，不能提前跳到下一个目标。"""
    from models.contracts import ReActDecision, TargetCriterionSettlement, TargetSettlementSubmission, TaskSpec

    class SettlementAdapter:
        def __init__(self):
            self.react_calls = 0
            self.settlement_calls = 0

        async def information_plan_or_fallback(self, **kwargs):
            return ExpertInformationPlan(targets=[InformationTarget(
                target_id="city-weather", objective="获取两座城市周末天气",
                completion_criteria=["南京天气", "苏州天气"],
            )])

        async def react_or_fallback(self, **kwargs):
            self.react_calls += 1
            if self.react_calls > 2:
                raise AssertionError("完整结算后不应继续当前目标")
            return ReActDecision(
                action="call_tool", public_summary="继续补齐天气", tool_name="weather",
                arguments={"city": "南京" if self.react_calls == 1 else "苏州"},
            )

        async def assess_tool_binding_or_fallback(self, **kwargs):
            from models.contracts import ToolBindingAssessment
            return ToolBindingAssessment(bound=True)

        async def assess_observation_or_fallback(self, **kwargs):
            return ObservationAssessment(
                relevance="relevant", summary="结果支持当前天气目标。", supports_current_target=True,
                coverage_contribution="partial",
            )

        async def settle_current_target_after_observation_or_none(self, **kwargs):
            self.settlement_calls += 1
            assert kwargs["current_target"]["completion_criteria"] == ["南京天气", "苏州天气"]
            assert "all_targets" not in kwargs
            if self.settlement_calls == 1:
                return TargetSettlementSubmission(
                    criteria=[
                        TargetCriterionSettlement(criterion="南京天气", satisfied=True),
                        TargetCriterionSettlement(criterion="苏州天气", satisfied=False, missing="缺少苏州天气"),
                    ],
                    coverage_status="partial", missing_information=["缺少苏州天气"],
                    target_complete=False, summary="仅取得南京天气。",
                )
            return TargetSettlementSubmission(
                criteria=[
                    TargetCriterionSettlement(criterion="南京天气", satisfied=True),
                    TargetCriterionSettlement(criterion="苏州天气", satisfied=True),
                ],
                coverage_status="full", missing_information=[], target_complete=True,
                summary="两座城市天气均已取得。",
            )

    class Gateway:
        def __init__(self):
            self.calls = 0

        async def call_tool(self, agent, tool_name, arguments, **kwargs):
            self.calls += 1
            return ToolObservation(
                call_id=f"call-{self.calls}", decision_id=kwargs["decision_id"], task_id=kwargs["task_id"],
                agent=agent, tool_name=tool_name, arguments=arguments, status=ToolCallStatus.SUCCEEDED,
                result_summary=f"{arguments['city']} 天气已取得。",
            )

    adapter, gateway = SettlementAdapter(), Gateway()
    context = AgentContext(
        decision_id="d", gateway=gateway, memory=MemoryContext(), model_adapter=adapter,
        request=DecisionRequest(query="比较南京和苏州周末旅游"),
    )
    asyncio.run(BaseReActAgent().execute(
        TaskSpec(task_id="weather", objective="比较周末天气", agent=AgentName.EVIDENCE_RESEARCH), context,
    ))

    assert gateway.calls == 2
    assert adapter.settlement_calls == 2
    assert context.information_targets[0]["status"] == "complete"
    assert context.information_targets[0]["missing_information"] == []
    assert len(context.information_targets[0]["settlement_criteria"]) == 2


def test_react_repair_message_is_consumed_once_for_its_own_target():
    """预检修正原因只服务同一 target 的下一轮，不得残留到后续 target。"""
    from models.contracts import ReActDecision, TaskSpec

    class Adapter:
        def __init__(self): self.messages = []
        async def react_or_fallback(self, **kwargs):
            self.messages.append(kwargs["execution_context"].get("react_validation_error"))
            return ReActDecision(action="request_replan", public_summary="停止")

    adapter = Adapter()
    context = AgentContext(
        decision_id="d", memory=MemoryContext(), model_adapter=adapter,
        request=DecisionRequest(query="测试"),
        execution_context={
            "active_information_target": {"target_id": "suzhou", "objective": "苏州天气", "completion_criteria": ["天气"]},
            "react_validation_error": {"target_id": "suzhou", "message": "城市参数不匹配"},
        },
    )
    task = TaskSpec(task_id="weather", objective="查询天气", agent=AgentName.EVIDENCE_RESEARCH)

    asyncio.run(BaseReActAgent()._decide(task, context, 3))

    assert adapter.messages == ["城市参数不匹配"]
    assert "react_validation_error" not in context.execution_context


def test_agent_result_excludes_historical_failures_and_keeps_only_final_target_gaps():
    """专家结果是总控长期输入，不能把已处理的调用失败当作事实缺口。"""
    from models.contracts import TaskSpec

    task = TaskSpec(task_id="weather", objective="查询天气", agent=AgentName.EVIDENCE_RESEARCH)
    context = AgentContext(
        decision_id="d", memory=MemoryContext(),
        information_targets=[{
            "target_id": "weekend", "objective": "苏州天气", "status": "partial",
            "missing_information": ["苏州周日降雨概率"], "tool_calls_used": 1,
        }],
        observations=[ToolObservation(
            call_id="failed-call", decision_id="d", task_id="weather", target_id="weekend",
            agent=AgentName.EVIDENCE_RESEARCH, tool_name="weather", status=ToolCallStatus.FAILED,
            error="历史参数错误：city=Nanjing",
        )],
    )

    result = asyncio.run(BaseReActAgent().finish(task, context, used=1))

    assert "历史参数错误：city=Nanjing" not in result.uncertainties
    assert "苏州周日降雨概率" in result.uncertainties


def test_general_agent_delegates_factual_work_then_synthesizes_results():
    """General 不直连 MCP，而是消费受限事实专家的委派结果再综合。"""
    from agents.general import GeneralAgent
    from models.contracts import GeneralDelegationPlan, GeneralDelegationRequest, GeneralTaskResolution, TaskSpec

    class Adapter:
        async def plan_general_delegations_or_fallback(self, **kwargs):
            return GeneralDelegationPlan(reason="缺少天气事实", delegations=[GeneralDelegationRequest(
                agent=AgentName.LOCATION_LIFESTYLE, work_kind="location_research",
                objective="查询苏州天气", completion_criteria=["周末天气"],
            )])
        async def resolve_general_task_or_fallback(self, **kwargs):
            assert kwargs["execution_context"]["delegated_results"][0]["findings"] == ["苏州周末晴"]
            return GeneralTaskResolution(summary="推荐苏州", findings=["综合结论"], uncertainties=[], completion_status="completed")

    async def delegate(parent, request, context):
        assert parent.agent is AgentName.GENERAL
        assert request.agent is AgentName.LOCATION_LIFESTYLE
        return {"findings": ["苏州周末晴"], "uncertainties": []}

    context = AgentContext(
        decision_id="d", memory=MemoryContext(), model_adapter=Adapter(), request=DecisionRequest(query="去哪里"),
        specialist_delegate=delegate,
    )
    result = asyncio.run(GeneralAgent().execute(TaskSpec(
        task_id="synthesis", objective="推荐目的地", agent=AgentName.GENERAL, work_kind="synthesis",
    ), context))

    assert result.findings == ["苏州周末晴", "综合结论"]
