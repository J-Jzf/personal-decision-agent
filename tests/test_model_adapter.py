import asyncio
import json

from app.config import Settings
from llm.adapter import ModelAdapter
from models.contracts import AgentName, DecisionRequest, DecisionReport, Evidence, MemoryContext, TaskSpec, ToolDescriptor, ToolObservation, ToolCallStatus


class BrokenCompletions:
    async def create(self, **kwargs):
        raise TimeoutError("offline")


class BrokenClient:
    class Chat:
        completions = BrokenCompletions()
    chat = Chat()


class PlannedCompletions:
    def __init__(self):
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        content = '''{
          "decision_type":"travel", "skill_name":"travel-destination-compare",
          "planning_summary":"比较天气与交通。",
          "plan":{"goal":"上海或贵州", "tasks":[{
            "task_id":"place", "objective":"查询天气", "agent":"location_lifestyle",
            "required_capabilities":["weather_forecast"]
          }]}
        }'''
        return type("Response", (), {"choices": [type("Choice", (), {"message": type("Message", (), {"content": content})()})()]})()


class PlannedClient:
    class Chat:
        completions = PlannedCompletions()
    chat = Chat()


class RepairingCompletions:
    def __init__(self): self.calls = 0
    async def create(self, **kwargs):
        self.calls += 1
        content = '{"decision_type":"travel"}' if self.calls == 1 else '''{"decision_type":"travel","skill_name":"travel-destination-compare","planning_summary":"比较天气。","plan":{"goal":"旅行","tasks":[]}}'''
        return type("Response", (), {"choices": [type("Choice", (), {"message": type("Message", (), {"content": content})()})()]})()


class RepairingClient:
    class Chat:
        completions = RepairingCompletions()
    chat = Chat()


class ProfileExtractionCompletions:
    def __init__(self):
        self.body = ""

    async def create(self, **kwargs):
        self.body = kwargs["messages"][1]["content"]
        content = '{"signals":["explicit:user_profile:career=software_engineer","explicit:user_profile:age=25","explicit:user_profile:hobby=hiking"]}'
        return type("Response", (), {"choices": [type("Choice", (), {"message": type("Message", (), {"content": content})()})()]})()


class ProfileExtractionClient:
    class Chat:
        completions = ProfileExtractionCompletions()
    chat = Chat()


class RepairingReactCompletions:
    def __init__(self):
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        content = (
            '{"action":"call_tool","public_summary":"准备查询天气"}'
            if len(self.calls) == 1
            else '{"action":"finish","public_summary":"现有证据不足，保留不确定性。","target_resolution":{"target_id":"weather-primary","status":"partial","summary":"已有部分天气资料，仍缺少比较范围。","missing_information":["完整天气范围"]}}'
        )
        return type("Response", (), {"choices": [type("Choice", (), {"message": type("Message", (), {"content": content})()})()]})()


class RepairingReactClient:
    class Chat:
        completions = RepairingReactCompletions()
    chat = Chat()


class RepairingJudgeCompletions:
    def __init__(self):
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        content = (
            '{"recommended_option":"上海"}'
            if len(self.calls) == 1
            else '{"recommended_option":"上海","confidence":0.7,"uncertainties":["天气仍需核验"]}'
        )
        return type("Response", (), {"choices": [type("Choice", (), {"message": type("Message", (), {"content": content})()})()]})()


class RepairingJudgeClient:
    class Chat:
        completions = RepairingJudgeCompletions()
    chat = Chat()


class TraceSummaryCompletions:
    def __init__(self):
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        content = '{"summary":"桂林周六晴朗，体感温度约 33°C；该结果仅覆盖天气维度。"}'
        return type("Response", (), {"choices": [type("Choice", (), {"message": type("Message", (), {"content": content})()})()]})()


class TraceSummaryClient:
    class Chat:
        completions = TraceSummaryCompletions()
    chat = Chat()


class EvidenceRelationshipCompletions:
    def __init__(self):
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        content = '{"relation":"complements","summary":"两条资料覆盖同一目标的不同细节。"}'
        return type("Response", (), {"choices": [type("Choice", (), {"message": type("Message", (), {"content": content})()})()]})()


class EvidenceRelationshipClient:
    class Chat:
        completions = EvidenceRelationshipCompletions()
    chat = Chat()


class ObservationAssessmentCompletions:
    """模拟模型把传输成功但语义无关的网页内容判为不可用。"""

    def __init__(self):
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        content = '{"relevance":"irrelevant","summary":"页面内容是北京景点，不能回答当前南京景点目标。","missing_information":["南京安静景点"]}'
        return type("Response", (), {"choices": [type("Choice", (), {"message": type("Message", (), {"content": content})()})()]})()


class ObservationAssessmentClient:
    class Chat:
        completions = ObservationAssessmentCompletions()
    chat = Chat()


class TargetSettlementCompletions:
    """模拟模型只补交当前目标的覆盖更新，不重试或改变专家的下一步动作。"""

    def __init__(self):
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        content = '''{"criteria":[{"criterion":"得到天气参考","satisfied":true}],"coverage_status":"full","missing_information":[],"target_complete":true,"summary":"已取得足以参考的天气资料。"}'''
        return type("Response", (), {"choices": [type("Choice", (), {"message": type("Message", (), {"content": content})()})()]})()


class TargetSettlementClient:
    class Chat:
        completions = TargetSettlementCompletions()
    chat = Chat()


class ToolBindingCompletions:
    def __init__(self):
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        content = '{"bound":false,"reason":"参数查询南京，不服务苏州天气目标。"}'
        return type("Response", (), {"choices": [type("Choice", (), {"message": type("Message", (), {"content": content})()})()]})()


class ToolBindingClient:
    class Chat:
        completions = ToolBindingCompletions()
    chat = Chat()


def test_failed_model_uses_local_reasoner_without_external_claims():
    async def scenario():
        adapter = ModelAdapter(Settings(llm_model_id="fake", llm_api_key="key"), client=BrokenClient())
        report = await adapter.judge_or_fallback(DecisionRequest(query="A or B", candidates=["A", "B"]))
        assert isinstance(report, DecisionReport)
        assert report.confidence <= 0.45
        assert report.confirmed_facts == []
        assert report.uncertainties
        assert adapter.fallback_events

    asyncio.run(scenario())


def test_local_reasoner_eliminates_explicit_constraint_violation():
    adapter = ModelAdapter(Settings(_env_file=None), client=None)
    report = asyncio.run(adapter.judge_or_fallback(DecisionRequest(
        query="选择预算内产品", candidates=["A", "B"],
        constraints=["B 不满足硬约束"], preferences=["偏好 A"],
    )))
    assert "B" in report.rejected_options
    assert report.recommended_option == "A"


def test_model_drives_skill_and_expert_plan_from_catalog_and_tool_schema():
    """自主规划必须使用模型结果，而不是关键词把旅行问题固定路由为工作 Offer。"""
    from llm.adapter import ModelAdapter

    adapter = ModelAdapter(Settings(llm_model_id="fake", llm_api_key="key"), client=PlannedClient())
    result = asyncio.run(adapter.autonomous_plan_or_fallback(
        DecisionRequest(query="上海和贵州周末旅游怎么选"),
        skills=[{"name": "travel-destination-compare", "description": "旅行", "workflow": ["weather"]}],
        tools=[ToolDescriptor(name="weather", capability="weather_forecast", description="天气", input_schema={"type": "object"}, allowed_agents=[AgentName.LOCATION_LIFESTYLE])],
        memory=MemoryContext(),
    ))

    assert result.skill_name == "travel-destination-compare"
    assert result.plan.tasks[0].agent is AgentName.LOCATION_LIFESTYLE
    plan_prompt = PlannedClient.chat.completions.calls[0]["messages"][0]["content"]
    assert "信息达到可支持结论的参考程度即可" in plan_prompt
    assert "不要在 plan 的最后生成“全局最终汇总/最终推荐”的 General task" in plan_prompt
    assert "保守推断" in plan_prompt
    assert "无需精确无误" in plan_prompt


def test_autonomous_plan_retries_invalid_json_contract_before_fallback():
    """首轮缺字段时，总控应收到校验错误并在五次内修正，不应立即降级。"""
    adapter = ModelAdapter(Settings(llm_model_id="fake", llm_api_key="key"), client=RepairingClient())
    result = asyncio.run(adapter.autonomous_plan_or_fallback(
        DecisionRequest(query="周末旅行"), skills=[{"name": "travel-destination-compare", "description": "旅行"}], tools=[], memory=MemoryContext(),
    ))
    assert result.decision_type.value == "travel"
    assert RepairingClient.chat.completions.calls == 2
    assert any(event["mode"] == "structured_retry" for event in adapter.fallback_events)


def test_profile_extraction_only_keeps_profile_signals_and_converts_age_to_birth_year_range():
    """用户明确年龄应变成出生年份范围，模型还必须收到时间上下文。"""
    async def scenario():
        adapter = ModelAdapter(Settings(llm_model_id="fake", llm_api_key="key"), client=ProfileExtractionClient())
        signals = await adapter.extract_user_profile_signals(
            texts=["我是软件工程师，今年 25 岁，爱好徒步。"],
            temporal_context={"timezone": "Asia/Shanghai", "reference_date": "2026-08-19", "expressions": []},
        )
        assert signals == [
            "explicit:user_profile:career=software_engineer",
            "explicit:user_profile:birth_year_range=2000-2001",
            "explicit:user_profile:hobby=hiking",
        ]
        assert '"temporal_context"' in ProfileExtractionClient.chat.completions.body

    asyncio.run(scenario())


def test_react_repairs_invalid_json_with_schema_tool_reference_and_user_information_boundary():
    """ReAct 首轮缺少工具能力时，应收到合同错误后修正，而不是直接本地降级。"""
    async def scenario():
        adapter = ModelAdapter(Settings(llm_model_id="fake", llm_api_key="key"), client=RepairingReactClient())
        decision = await adapter.react_or_fallback(
            task=TaskSpec(
                task_id="react-hidden-task-id", objective="查询天气", agent=AgentName.LOCATION_LIFESTYLE,
                dependencies=["upstream-task"], required_capabilities=["weather_forecast"],
                allow_factual_delegation=True,
            ),
            request=DecisionRequest(query="这周末上海天气如何？", candidates=["仅供上游候选-南京", "仅供上游候选-苏州"]),
            memory=MemoryContext(), observations=[],
            tools=[ToolDescriptor(
                name="weather", capability="weather_forecast", description="查询指定城市和日期的天气预报",
                input_schema={"type": "object", "required": ["location"]},
                allowed_agents=[AgentName.LOCATION_LIFESTYLE],
            )],
            remaining_calls=3,
            execution_context={
                "current_task_history": [{"tool_name": "weather", "status": "succeeded", "result_summary": "桂林天气已获取"}],
                "active_information_target": {
                    "target_id": "shanghai-weather", "objective": "获取上海 8 月 23 日天气",
                    "completion_criteria": ["上海 8 月 23 日天气"], "missing_information": ["上海 8 月 23 日天气"],
                },
            },
        )

        calls = RepairingReactClient.chat.completions.calls
        assert decision.action == "finish"
        assert all(event["mode"] == "structured_retry" for event in adapter.fallback_events)
        assert len(calls) == 2
        assert "ReActDecision" in calls[0]["messages"][0]["content"]
        assert "不要编造用户信息" in calls[0]["messages"][0]["content"]
        assert "查询指定城市和日期的天气预报" in calls[0]["messages"][1]["content"]
        assert "上海 8 月 23 日天气" in calls[0]["messages"][1]["content"]
        assert "用户问题" in calls[0]["messages"][1]["content"]
        assert "查询天气" not in calls[0]["messages"][1]["content"]
        assert "react-hidden-task-id" not in calls[0]["messages"][1]["content"]
        assert "upstream-task" not in calls[0]["messages"][1]["content"]
        assert "仅供上游候选-南京" not in calls[0]["messages"][1]["content"]
        assert "仅供上游候选-苏州" not in calls[0]["messages"][1]["content"]
        assert "当前 target completion_criteria" in calls[0]["messages"][1]["content"]
        assert "当前 target 已有 observations" in calls[0]["messages"][1]["content"]
        assert "信息达到可支持结论的参考程度即可" in calls[0]["messages"][0]["content"]
        assert "保守推断" in calls[0]["messages"][0]["content"]
        assert "逐项检查完成条件" in calls[0]["messages"][0]["content"]
        assert "all_tasks" not in calls[0]["messages"][1]["content"]
        assert "previous_validation_error" in calls[1]["messages"][1]["content"]

    asyncio.run(scenario())


def test_judge_repairs_invalid_json_with_full_schema_and_user_information_boundary():
    """Judge 首轮缺少置信度时，应通过合同修正返回模型报告，而不是降级。"""
    async def scenario():
        adapter = ModelAdapter(Settings(llm_model_id="fake", llm_api_key="key"), client=RepairingJudgeClient())
        report = await adapter.judge_or_fallback(DecisionRequest(query="上海还是贵州", candidates=["上海", "贵州"]))

        calls = RepairingJudgeClient.chat.completions.calls
        assert report.analysis_mode.value == "model"
        assert report.confidence == 0.7
        assert len(calls) == 2
        assert "DecisionReport" in calls[0]["messages"][0]["content"]
        assert "不要编造用户信息" in calls[0]["messages"][0]["content"]
        assert "信息达到可支持结论的参考程度即可" in calls[0]["messages"][0]["content"]
        assert "保守推断" in calls[0]["messages"][0]["content"]
        assert "输出前自行检查" in calls[0]["messages"][0]["content"]
        assert "未找到证据" in calls[0]["messages"][0]["content"]
        assert "previous_validation_error" in calls[1]["messages"][1]["content"]

    asyncio.run(scenario())


def test_default_hitl_timeout_is_thirty_seconds():
    """默认等待时间应给用户足够时间阅读和填写动态表单。"""
    assert Settings(_env_file=None).hitl_timeout_seconds == 30
    assert Settings(_env_file=None).replan_limit == 3


def test_trace_summary_uses_question_and_tool_result_to_extract_public_key_points():
    """长工具结果应由模型提炼关键事实，而不是直接截断开头。"""
    async def scenario():
        adapter = ModelAdapter(Settings(llm_model_id="fake", llm_api_key="key"), client=TraceSummaryClient())
        summary = await adapter.summarize_tool_result(
            query="这周末上海和桂林选哪个旅游？", task_objective="比较两地周末天气",
            tool_name="get_weather_byDateTimeRange", raw_result="字段说明" * 5000,
        )

        assert "桂林周六晴朗" in summary
        request_body = TraceSummaryClient.chat.completions.calls[0]["messages"][1]["content"]
        assert len(json.loads(request_body)["raw_result"]) == 16000
        assert "上海和桂林" in request_body
        assert "get_weather_byDateTimeRange" in request_body

    asyncio.run(scenario())


def test_observation_assessment_uses_model_semantics_instead_of_transport_success():
    """MCP 正常返回北京网页时，语义核验应判定它不支持南京目标。"""
    async def scenario():
        adapter = ModelAdapter(Settings(llm_model_id="fake", llm_api_key="key"), client=ObservationAssessmentClient())
        observation = ToolObservation(
            call_id="c", decision_id="d", task_id="attractions", agent=AgentName.EVIDENCE_RESEARCH,
            tool_name="fetch_page", arguments={"url": "https://example.test/beijing"},
            status=ToolCallStatus.SUCCEEDED, result_summary="北京丫髻山景点介绍。",
        )
        assessment = await adapter.assess_observation_or_fallback(
            request=DecisionRequest(query="南京和苏州，哪个更适合安静散步？"),
            task=TaskSpec(task_id="attractions", objective="比较南京与苏州安静景点", agent=AgentName.EVIDENCE_RESEARCH),
            target={"target_id": "nanjing-spots", "objective": "获取南京安静景点", "completion_criteria": ["至少一个南京景点"]},
            observation=observation,
        )

        assert assessment.relevance == "irrelevant"
        assert "北京" in assessment.summary
        prompt = ObservationAssessmentClient.chat.completions.calls[0]["messages"][0]["content"]
        assert "不得按关键词或传输状态猜测相关性" in prompt
        assert "完全无关" in prompt

    asyncio.run(scenario())


def test_observation_assessment_falls_back_to_unverifiable_when_model_is_unavailable():
    """没有模型时不得把 MCP 成功直接伪装为语义相关证据。"""
    adapter = ModelAdapter(Settings(_env_file=None), client=None)
    assessment = asyncio.run(adapter.assess_observation_or_fallback(
        request=DecisionRequest(query="比较两个城市"),
        task=TaskSpec(task_id="research", objective="检索资料", agent=AgentName.EVIDENCE_RESEARCH),
        target={"target_id": "research-primary", "objective": "检索资料", "completion_criteria": []},
        observation=ToolObservation(
            call_id="c", decision_id="d", task_id="research", agent=AgentName.EVIDENCE_RESEARCH,
            tool_name="search", status=ToolCallStatus.SUCCEEDED, result_summary="某条工具返回。",
        ),
    ))
    assert assessment.relevance == "unverifiable"


def test_tool_binding_preflight_uses_only_unfinished_current_criteria_and_selected_action():
    """预检只需证明调用可补当前未满足 criterion，不能要求单次完成整个 target。"""
    async def scenario():
        adapter = ModelAdapter(Settings(llm_model_id="fake", llm_api_key="key"), client=ToolBindingClient())
        assessment = await adapter.assess_tool_binding_or_fallback(
            request=DecisionRequest(query="南京和苏州周末旅游怎么选", constraints=["预算 3000"]),
            task=TaskSpec(task_id="weather", objective="比较周末天气", agent=AgentName.LOCATION_LIFESTYLE),
            target={
                "target_id": "suzhou-weather", "objective": "获取苏州周末信息",
                "completion_criteria": ["苏州位置已确认", "苏州天气"],
                "settlement_criteria": [
                    {"criterion": "苏州位置已确认", "satisfied": True},
                    {"criterion": "苏州天气", "satisfied": False, "missing": "周末天气"},
                ],
                "missing_information": ["周末天气"],
            },
            tool={"name": "weather", "input_schema": {"type": "object"}}, arguments={"city": "Nanjing"},
        )

        body = ToolBindingClient.chat.completions.calls[0]["messages"][1]["content"]
        assert assessment.bound is False
        assert "苏州天气" in body
        assert "Nanjing" in body
        assert "known_information_targets" not in body
        prompt = ToolBindingClient.chat.completions.calls[0]["messages"][0]["content"]
        assert "尚未完成 criterion" in prompt
        assert "单次调用独立完成整个 target" in prompt

    asyncio.run(scenario())


def test_target_settlement_submission_uses_one_call_with_current_completion_criteria():
    """每条当前目标有效观察都应进入独立结算，并逐项检查完成条件。"""
    async def scenario():
        adapter = ModelAdapter(Settings(llm_model_id="fake", llm_api_key="key"), client=TargetSettlementClient())
        submission = await adapter.settle_current_target_after_observation_or_none(
            current_target={"target_id": "weather-primary", "objective": "获取周末天气", "completion_criteria": ["得到天气参考"]},
            target_observations=[{"call_id": "weather-call", "result_summary": "甲地周末晴朗。", "semantic_status": "relevant"}],
            existing_coverage={},
        )

        calls = TargetSettlementClient.chat.completions.calls
        assert submission is not None
        assert submission.target_complete is True
        assert len(calls) == 1
        assert "TargetSettlementSubmission" in calls[0]["messages"][0]["content"]
        assert "target_complete" in calls[0]["messages"][0]["content"]
        assert "weather-call" in calls[0]["messages"][1]["content"]
        assert "completion_criteria" in calls[0]["messages"][1]["content"]
        assert "逐条判断 criteria 是否满足" in calls[0]["messages"][0]["content"]

    asyncio.run(scenario())


def test_same_scope_evidence_uses_structured_model_relationship_judgement():
    """只有同范围资料才交给模型判断补充或矛盾，避免文本不同自动冲突。"""
    async def scenario():
        adapter = ModelAdapter(Settings(llm_model_id="fake", llm_api_key="key"), client=EvidenceRelationshipClient())
        relation = await adapter.evidence_relationship_or_fallback(
            left=Evidence(evidence_id="left", decision_id="d", claim="天气", scope_key="weather:nanjing", value="上午有雨"),
            right=Evidence(evidence_id="right", decision_id="d", claim="天气", scope_key="weather:nanjing", value="下午转晴"),
        )

        assert relation.relation == "complements"
        prompt = EvidenceRelationshipClient.chat.completions.calls[0]["messages"][0]["content"]
        assert "同一 scope_key" in prompt
        assert "contradicts" in prompt

    asyncio.run(scenario())
