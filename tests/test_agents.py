import asyncio

from agents.base import AgentContext, BaseReActAgent, ToolAction
from agents.planner import Planner
from models.contracts import AgentName, DecisionRequest, DecisionType, ExpertInformationPlan, InformationCoverageUpdate, InformationTarget, MemoryContext, ObservationAssessment, ObservationRelevance, TargetResolution, TaskStatus, ToolCallStatus, ToolObservation
from skills.registry import SkillRegistry


def test_react_context_keeps_successful_information_and_only_exposes_the_latest_failure():
    """模型上下文应继承成功资料，历史失败原因只在它恰好是上一轮时才暴露。"""
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
        },
    )

    prompt_history = BaseReActAgent._prompt_task_history(observations)
    view = BaseReActAgent._react_context_view(task, context, prompt_history, {})

    assert view["当前任务本轮执行期间此前的全部工具调用"][0]["status"] == "failed"
    assert "error" not in view["当前任务本轮执行期间此前的全部工具调用"][0]
    assert view["成功摘要"] == ["苏州天气已获得"]
    assert view["已经获得的所有信息结果"] == [{"task_id": "earlier", "result_summary": "南京天气已获得"}]
    assert view["跨任务和重规划继承的结构化覆盖状态"]["weather"]["covered_locations"] == ["南京"]
    assert "上一轮失败原因" not in view

    failed_last = BaseReActAgent._react_context_view(task, context, observations[:1], {})
    assert failed_last["上一轮失败原因"] == "temporary error"


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


def test_react_context_exposes_referenceable_verified_evidence_for_a_derived_target():
    """纯归纳目标必须看到可引用的 call_id，模型才能结算前序资料而不重复调用工具。"""
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

    assert view["可引用证据账本"][0]["call_id"] == "call-a"


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
            assert kwargs["current_target"]["target_id"] == "nanjing"
            return __import__("types").SimpleNamespace(
                coverage_updates=[InformationCoverageUpdate(
                    target_key="nanjing", target="南京资料", status="complete", summary="南京资料已获得",
                )],
                target_resolution=None,
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
    from types import SimpleNamespace
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
            self.settlement_requests += 1
            assert kwargs["current_target"]["target_id"] == "city-weather"
            assert "completion_criteria" not in kwargs["current_target"]
            assert kwargs["tool_observation"]["tool_name"] == "weather"
            assert kwargs["tool_observation"]["semantic_status"] == "relevant"
            return SimpleNamespace(
                coverage_updates=[InformationCoverageUpdate(
                    target_key="city-weather", target="获取城市周末天气", status="complete", summary="天气资料已经足够参考。",
                )],
                target_resolution=None,
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


def test_react_can_complete_a_comparison_target_by_referencing_prior_verified_observations():
    """比较目标可引用同一专家先前资料，不能因自己未调用工具而被错误阻塞。"""
    from models.contracts import ReActDecision, TaskSpec

    class DerivedConclusionAdapter:
        def __init__(self):
            self.turn = 0

        async def information_plan_or_fallback(self, **kwargs):
            return ExpertInformationPlan(targets=[
                InformationTarget(target_id="option-a", objective="获取选项 A 条件", completion_criteria=["得到 A 条件"]),
                InformationTarget(target_id="option-b", objective="获取选项 B 条件", completion_criteria=["得到 B 条件"]),
                InformationTarget(target_id="comparison", objective="基于 A、B 条件形成比较", completion_criteria=["完成比较"]),
            ])

        async def react_or_fallback(self, **kwargs):
            self.turn += 1
            decisions = {
                1: ReActDecision(action="call_tool", public_summary="查询 A", tool_name="search", arguments={"query": "A"}),
                2: ReActDecision(action="finish", public_summary="A 已获得", target_resolution=TargetResolution(
                    target_id="option-a", status="complete", summary="A 条件已获得。",
                )),
                3: ReActDecision(action="call_tool", public_summary="查询 B", tool_name="search", arguments={"query": "B"}),
                4: ReActDecision(action="finish", public_summary="B 已获得", target_resolution=TargetResolution(
                    target_id="option-b", status="complete", summary="B 条件已获得。",
                )),
                5: ReActDecision(action="finish", public_summary="比较已完成", target_resolution=TargetResolution(
                    target_id="comparison", status="complete", summary="已基于 A、B 条件完成保守比较。",
                    evidence_refs=["call-a", "call-b"], reasoning_basis="conservative_inference",
                )),
            }
            return decisions[self.turn]

        async def assess_observation_or_fallback(self, **kwargs):
            return ObservationAssessment(relevance="relevant", summary="结果直接支持当前目标。")

    class TwoFactGateway:
        def __init__(self):
            self.calls = 0

        async def call_tool(self, agent, tool_name, arguments, **kwargs):
            self.calls += 1
            return ToolObservation(
                call_id="call-a" if self.calls == 1 else "call-b", decision_id=kwargs["decision_id"],
                task_id=kwargs["task_id"], agent=agent, tool_name=tool_name, arguments=arguments,
                status=ToolCallStatus.SUCCEEDED, result_summary=f"{arguments['query']} 的已验证资料",
            )

    gateway = TwoFactGateway()
    context = AgentContext(
        decision_id="d", gateway=gateway, memory=MemoryContext(), model_adapter=DerivedConclusionAdapter(),
        request=DecisionRequest(query="比较 A 和 B"),
    )
    result = asyncio.run(BaseReActAgent().execute(
        TaskSpec(task_id="research", objective="比较 A 和 B", agent=AgentName.EVIDENCE_RESEARCH), context,
    ))

    assert gateway.calls == 2
    assert context.information_targets[-1]["status"] == "complete"
    assert result.completion_status is TaskStatus.COMPLETED


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
