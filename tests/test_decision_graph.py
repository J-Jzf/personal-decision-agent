import asyncio

from app.container import build_services
from app.config import Settings
from models.contracts import DecisionRequest, WorkflowStatus
from graph.routing import route_after_execution
from graph.states import DecisionState
from evidence.pool import EvidencePool


def test_model_unavailable_keeps_missing_evidence_without_automatic_replan_and_resume_uses_saved_state(tmp_path):
    """离线降级不能因每个普通缺口重复规划；仍须能够从保存状态继续。"""
    services = build_services(Settings(_env_file=None, MCP_COMMANDS_JSON=[], sqlite_path=tmp_path / "db.sqlite", qdrant_path=tmp_path / "qdrant"))
    first = asyncio.run(services.graph.run(DecisionRequest(query="比较两款电脑", candidates=["A", "B"])))
    assert WorkflowStatus.REPLANNING not in [event.to_state for event in first.events]
    resumed = asyncio.run(services.graph.continue_decision(first.decision_id, "继续并使用已有资料"))
    assert resumed.decision_id == first.decision_id
    assert resumed.status == WorkflowStatus.ARCHIVED


def test_workflow_archives_only_after_report(tmp_path):
    services = build_services(Settings(_env_file=None, MCP_COMMANDS_JSON=[], sqlite_path=tmp_path / "db.sqlite", qdrant_path=tmp_path / "qdrant"))
    response = asyncio.run(services.graph.run(DecisionRequest(query="A or B", candidates=["A", "B"])))
    assert response.report is not None
    assert services.archives.get(response.decision_id).status == WorkflowStatus.ARCHIVED


def test_service_graph_does_not_keep_an_unused_supervisor_dependency(tmp_path):
    """主工作流应只装配实际参与决策的组件，不能保留未调用的主管 Agent。"""
    services = build_services(Settings(
        _env_file=None, MCP_COMMANDS_JSON=[], sqlite_path=tmp_path / "db.sqlite", qdrant_path=tmp_path / "qdrant",
    ))

    assert not hasattr(services.graph, "supervisor")


def test_initial_user_input_profile_is_written_to_the_archived_episode(tmp_path):
    """初始问题中的用户自述必须在归档后以显式信号进入长期画像。"""
    services = build_services(Settings(
        _env_file=None, sqlite_path=tmp_path / "db.sqlite", qdrant_path=tmp_path / "qdrant",
        MCP_COMMANDS_JSON=[],
    ))

    async def extract_user_profile_signals(**kwargs):
        assert "软件工程师" in kwargs["texts"][0]
        assert kwargs["temporal_context"]["expressions"]
        return ["explicit:user_profile:career=software_engineer"]

    services.graph.judge.model_adapter.extract_user_profile_signals = extract_user_profile_signals
    response = asyncio.run(services.graph.run(DecisionRequest(query="我是软件工程师，这周末上海和贵州去哪里旅游？")))

    episode = services.memory.episodes.by_decision_id(response.decision_id)
    profile = services.memory.profile_for("user_profile", "career")
    assert episode is not None and episode.profile_signals == ["explicit:user_profile:career=software_engineer"]
    assert profile is not None and profile.value == "software_engineer"


def test_hitl_user_text_is_retained_for_profile_extraction_and_date_context():
    """HITL 也是用户输入，必须保留其画像文本并解析其中的相对日期。"""
    from graph.decision_graph import DecisionGraph

    request = DecisionGraph._with_user_texts(
        DecisionRequest(query="比较两个 Offer"),
        ["我住在上海，明天可以面试；我喜欢徒步。"],
        extra_context={"hitl": {"free_text": "我住在上海，明天可以面试；我喜欢徒步。"}},
    )

    assert request.context["profile_source_texts"] == ["我住在上海，明天可以面试；我喜欢徒步。"]
    expressions = request.context["temporal_context"]["expressions"]
    assert len(expressions) == 1
    assert expressions[0]["raw"] == "明天"
    assert expressions[0]["kind"] == "date"


def test_replanning_remains_available_until_the_configured_limit_is_reached():
    """一次重规划后仍有失败任务时，应允许继续请求替代计划。"""
    from models.contracts import AgentName, AgentResult, ExecutionPlan, TaskSpec, TaskStatus
    state = DecisionState(
        decision_id="d", request=DecisionRequest(query="A 或 B"), replan_count=1,
        agent_results=[AgentResult(
            result_id="r", decision_id="d", task_id="search", agent_name=AgentName.EVIDENCE_RESEARCH,
            completion_status=TaskStatus.BLOCKED, uncertainties=["search unavailable"],
        )],
        plan=ExecutionPlan(tasks=[TaskSpec(task_id="search", objective="检索资料", agent=AgentName.EVIDENCE_RESEARCH)]),
    )
    class ConservativeController:
        async def replan_decision_or_fallback(self, **kwargs):
            from models.contracts import ReplanDecision
            return ReplanDecision(
                should_replan=False, reason="资料不足但不会实质改变当前结论。",
                critical_gaps=[], can_execute_remedy=False,
            )

    assert asyncio.run(route_after_execution(state, EvidencePool(), ConservativeController())) == "judge"


def test_route_replans_only_when_controller_marks_a_gap_critical_and_remediable():
    """blocked 信息目标不应自行触发循环；是否补救由总控结构化判断。"""
    from models.contracts import AgentName, AgentResult, ExecutionPlan, ReplanDecision, TaskSpec, TaskStatus
    state = DecisionState(
        decision_id="d", request=DecisionRequest(query="A 或 B"),
        agent_results=[AgentResult(
            result_id="r", decision_id="d", task_id="weather", agent_name=AgentName.LOCATION_LIFESTYLE,
            completion_status=TaskStatus.BLOCKED, uncertainties=["天气资料只覆盖一天"],
        )],
        plan=ExecutionPlan(tasks=[TaskSpec(task_id="weather", objective="查询天气", agent=AgentName.LOCATION_LIFESTYLE)]),
    )

    class Controller:
        async def replan_decision_or_fallback(self, **kwargs):
            return ReplanDecision(
                should_replan=True, reason="缺少另一候选项天气会改变推荐。",
                critical_gaps=["另一候选项天气"], can_execute_remedy=True,
            )

    assert asyncio.run(route_after_execution(state, EvidencePool(), Controller())) == "replan"
from agents.base import AgentContext
from graph.decision_graph import DecisionGraph
from graph.states import DecisionState
from models.contracts import AgentName, AgentResult, DecisionRequest, ExecutionPlan, TaskSpec, TaskStatus, ToolCallStatus, ToolObservation


def test_incremental_replan_removes_previously_completed_tasks_and_keeps_new_work():
    """重规划只应执行尚未完成的任务，不得重跑已成功完成的部分。"""
    state = DecisionState(
        decision_id="d", request=DecisionRequest(query="上海还是桂林"),
        plan=ExecutionPlan(tasks=[TaskSpec(task_id="weather", objective="查询天气", agent=AgentName.LOCATION_LIFESTYLE)]),
        agent_results=[AgentResult(
            result_id="r", decision_id="d", task_id="weather", agent_name=AgentName.LOCATION_LIFESTYLE,
            completion_status=TaskStatus.COMPLETED,
        )],
    )
    replacement = ExecutionPlan(tasks=[
        TaskSpec(task_id="weather", objective="查询天气", agent=AgentName.LOCATION_LIFESTYLE),
        TaskSpec(task_id="transport", objective="查询交通", agent=AgentName.EVIDENCE_RESEARCH, dependencies=["weather"]),
    ])

    remaining, skipped = DecisionGraph.incremental_replan_tasks(state, replacement)

    assert [task.task_id for task in remaining] == ["transport"]
    assert remaining[0].dependencies == []
    assert skipped == ["weather"]


def test_blocked_task_does_not_satisfy_a_downstream_dependency():
    """仅已完成专家任务可解锁依赖任务；blocked 结果必须进入重规划或最终不确定性。"""
    blocked = AgentResult(
        result_id="r", decision_id="d", task_id="weather", agent_name=AgentName.LOCATION_LIFESTYLE,
        completion_status=TaskStatus.BLOCKED,
    )
    completed = AgentResult(
        result_id="r2", decision_id="d", task_id="preference", agent_name=AgentName.PREFERENCE,
        completion_status=TaskStatus.COMPLETED,
    )

    assert DecisionGraph.satisfied_task_ids([blocked, completed]) == {"preference"}


def test_completed_with_gaps_task_satisfies_an_ordinary_downstream_dependency():
    """已有可用资料的任务应解锁普通综合任务，并由总控在报告中保留缺口。"""
    usable_with_gaps = AgentResult(
        result_id="r", decision_id="d", task_id="research", agent_name=AgentName.EVIDENCE_RESEARCH,
        findings=["已获得一项可比较资料"], uncertainties=["另一维度仅有粗略参考"],
        completion_status=TaskStatus.COMPLETED_WITH_GAPS,
    )

    assert DecisionGraph.satisfied_task_ids([usable_with_gaps]) == {"research"}


def test_weather_coverage_is_carried_into_new_tasks_after_replanning():
    """成功天气观察必须按城市结构化继承，不能在重规划后被遗忘。"""
    state = DecisionState(
        decision_id="d", request=DecisionRequest(query="南京和苏州周末旅游", candidates=["南京", "苏州"]),
        plan=ExecutionPlan(tasks=[TaskSpec(task_id="weather_old", objective="查询两地天气", agent=AgentName.LOCATION_LIFESTYLE)]),
        tool_observations=[ToolObservation(
            call_id="1", decision_id="d", task_id="weather_old", agent=AgentName.LOCATION_LIFESTYLE,
            tool_name="get_weather", arguments={"location": "南京"},
            status=ToolCallStatus.SUCCEEDED, result_summary="南京周末天气已获得",
        )],
    )

    context = DecisionGraph._react_execution_context(
        state, TaskSpec(task_id="weather_new", objective="补齐两地天气", agent=AgentName.LOCATION_LIFESTYLE), EvidencePool(),
    )

    coverage = context["structured_coverage"]["weather"]
    assert coverage["covered_locations"] == ["南京"]
    assert coverage["missing_locations"] == ["苏州"]
    assert context["all_successful_information"][0]["result_summary"] == "南京周末天气已获得"


def test_semantic_information_coverage_is_carried_into_replanned_task_context():
    """任务重规划后，专家仍须看到之前部分或完整信息目标的最新有效状态。"""
    state = DecisionState(
        decision_id="d", request=DecisionRequest(query="比较两个方案"),
        information_coverage={
            "option-comparison": {
                "target": "比较两个方案的关键条件",
                "status": "complete",
                "latest_summary": "两个方案的关键资料已经齐全。",
                "history": [{"status": "partial", "summary": "先取得部分资料。"}],
            }
        },
    )

    context = DecisionGraph._react_execution_context(
        state, TaskSpec(task_id="new-task", objective="形成比较结论", agent=AgentName.EVIDENCE_RESEARCH), EvidencePool(),
    )

    assert context["information_coverage"]["option-comparison"]["status"] == "complete"


def test_progress_summary_has_controller_context_after_a_task_completes():
    """每个任务完成后，总控必须得到可审计的进度摘要。"""
    state = DecisionState(
        decision_id="d", request=DecisionRequest(query="上海还是桂林"),
        plan=ExecutionPlan(tasks=[TaskSpec(task_id="weather", objective="查询两地天气", agent=AgentName.LOCATION_LIFESTYLE)]),
        agent_results=[AgentResult(
            result_id="r", decision_id="d", task_id="weather", agent_name=AgentName.LOCATION_LIFESTYLE,
            findings=["桂林天气已取得"], completion_status=TaskStatus.COMPLETED,
        )],
    )

    summary = DecisionGraph.build_progress_summary(state, EvidencePool(), state.agent_results[0])

    assert summary["用户问题"] == "上海还是桂林"
    assert summary["目前已完成的任务有"][0]["task_id"] == "weather"
    assert summary["目前得到的信息"] == ["桂林天气已取得"]
    assert "下一步应该做" in summary


def test_replan_without_an_executable_task_is_rejected():
    """重规划不能只输出缺口说明；无任务时应直接转入后续判断。"""
    assert not DecisionGraph.has_executable_replan_tasks([], ["上海 23 日天气缺失"])


def test_risk_critic_completion_has_a_visible_trace_event(tmp_path):
    """风险专家的发现与不确定性必须作为前端可见轨迹保存。"""
    services = build_services(Settings(_env_file=None, sqlite_path=tmp_path / "db.sqlite", qdrant_path=tmp_path / "qdrant", MCP_COMMANDS_JSON=[]))
    response = asyncio.run(services.graph.run(DecisionRequest(query="比较两款电脑", candidates=["A", "B"])))

    event = next(item for item in response.events if item.kind == "risk_review_completed")

    assert event.payload["agent"] == "risk_critic"
    assert "findings" in event.payload
