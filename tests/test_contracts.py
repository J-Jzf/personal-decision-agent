import pytest
from pydantic import ValidationError


def test_report_requires_explicit_evidence_categories():
    """Removing report category defaults would hide unclassified evidence."""
    from models.contracts import DecisionReport

    report = DecisionReport(recommended_option="A", confidence=0.4)

    assert report.confirmed_facts == []
    assert report.uncertainties == []


def test_settings_use_safe_local_defaults():
    """Changing runtime defaults must not redirect local persistence unexpectedly."""
    from app.config import Settings

    settings = Settings(_env_file=None)

    assert settings.sqlite_path.name == "personal_decision.db"
    assert settings.qdrant_path.name == "qdrant"
    assert settings.request_timeout_seconds > 0
    assert settings.tool_timeout_seconds > 0
    assert settings.react_call_limit == 3


def test_execution_plan_rejects_duplicate_task_ids():
    """Allowing duplicate task IDs would make task results ambiguous."""
    from models.contracts import ExecutionPlan, TaskSpec

    tasks = [TaskSpec(task_id="research", objective="Find facts"), TaskSpec(task_id="research", objective="Compare facts")]

    with pytest.raises(ValidationError, match="unique"):
        ExecutionPlan(tasks=tasks)


def test_execution_plan_rejects_unknown_dependencies():
    """Allowing a missing dependency would make the workflow impossible to schedule."""
    from models.contracts import ExecutionPlan, TaskSpec

    with pytest.raises(ValidationError, match="unknown task"):
        ExecutionPlan(tasks=[TaskSpec(task_id="judge", objective="Choose", dependencies=["research"])])


def test_evidence_only_accepts_contract_statuses():
    """An unrecognized evidence status would bypass verification policy."""
    from models.contracts import Evidence

    evidence = Evidence(evidence_id="ev-1", decision_id="d-1", claim="Price is published", status="confirmed")

    assert evidence.status.value == "confirmed"
    with pytest.raises(ValidationError):
        Evidence(evidence_id="ev-2", decision_id="d-1", claim="Bad", status="unchecked")


def test_decision_request_requires_nonempty_query():
    """An empty decision query cannot be routed to a decision skill."""
    from models.contracts import DecisionRequest

    with pytest.raises(ValidationError):
        DecisionRequest(query="   ")


@pytest.mark.parametrize("field_name", ["supports", "contradicts"])
def test_evidence_rejects_blank_semantic_references(field_name):
    """Blank evidence references would make evidence relationships unresolvable."""
    from models.contracts import Evidence

    with pytest.raises(ValidationError, match="identifiers must be non-empty"):
        Evidence(
            evidence_id="ev-1",
            decision_id="d-1",
            claim="Published price",
            **{field_name: [" "]},
        )


def test_agent_result_rejects_blank_evidence_ids():
    """A blank evidence ID would make an agent handoff impossible to trace."""
    from models.contracts import AgentResult

    with pytest.raises(ValidationError, match="identifiers must be non-empty"):
        AgentResult(result_id="result-1", decision_id="d-1", task_id="task-1", agent_name="judge", evidence_ids=[""])


def test_profile_memory_rejects_blank_source_episode_ids():
    """A blank source episode ID would sever the memory provenance chain."""
    from models.contracts import ProfileMemory

    with pytest.raises(ValidationError, match="identifiers must be non-empty"):
        ProfileMemory(
            memory_id="memory-1",
            category="budget",
            memory_key="price_sensitivity",
            value=True,
            importance=0.5,
            confidence=0.5,
            source_episode_ids=["  "],
        )


def test_mutable_contract_defaults_are_isolated():
    """Sharing a default evidence category would leak one decision into another."""
    from models.contracts import DecisionReport

    first = DecisionReport(recommended_option="A", confidence=0.4)
    second = DecisionReport(recommended_option="B", confidence=0.6)
    first.confirmed_facts.append("verified")

    assert second.confirmed_facts == []


@pytest.mark.parametrize("confidence", [0, 1])
def test_report_accepts_probability_boundaries(confidence):
    """Rejecting inclusive probability bounds would reject valid certainty values."""
    from models.contracts import DecisionReport

    assert DecisionReport(recommended_option="A", confidence=confidence).confidence == confidence


@pytest.mark.parametrize("confidence", [-0.01, 1.01])
def test_report_rejects_probabilities_outside_boundaries(confidence):
    """An out-of-range confidence would misrepresent decision certainty."""
    from models.contracts import DecisionReport

    with pytest.raises(ValidationError):
        DecisionReport(recommended_option="A", confidence=confidence)


def test_evidence_rejects_empty_identifier():
    """An empty evidence identifier would make evidence persistence ambiguous."""
    from models.contracts import Evidence

    with pytest.raises(ValidationError):
        Evidence(evidence_id="", decision_id="d-1", claim="Published price")


def test_autonomous_plan_and_hitl_contracts_preserve_model_selected_workflow():
    """总控模型应能用结构化契约选择 Skill、专家任务和需要补充的字段。"""
    from models.contracts import AgentName, AutonomousPlan, HITLField, HITLRequest

    plan = AutonomousPlan.model_validate({
        "decision_type": "travel", "skill_name": "travel-destination-compare",
        "planning_summary": "需要比较周末天气、交通与旅行节奏。",
        "plan": {"goal": "上海或贵州", "tasks": [{
            "task_id": "travel_research", "objective": "比较两地天气", "agent": "location_lifestyle",
            "required_capabilities": ["weather_forecast", "web_search"],
        }]},
    })
    request = HITLRequest(
        request_id="h-1", decision_id="d-1", source_agent=AgentName.PLANNER,
        stage="planning", question="请补充旅行预算", rationale="预算会影响交通和住宿建议。",
        fields=[HITLField(key="budget", label="总预算", placeholder="例如：3000 元")],
    )

    assert plan.skill_name == "travel-destination-compare"
    assert plan.plan.tasks[0].agent is AgentName.LOCATION_LIFESTYLE
    assert request.fields[0].key == "budget"


def test_target_resolution_accepts_verified_evidence_references_for_a_derived_conclusion():
    """归纳目标应能引用此前核验过的资料，而不必虚构一次新的工具调用。"""
    from models.contracts import TargetResolution

    resolution = TargetResolution(
        target_id="comparison", status="complete", summary="已基于两项资料完成比较。",
        evidence_refs=["call-nanjing", "call-suzhou"], reasoning_basis="conservative_inference",
    )

    assert resolution.evidence_refs == ["call-nanjing", "call-suzhou"]
    assert resolution.reasoning_basis == "conservative_inference"


def test_agent_result_accepts_completed_with_gaps_when_evidence_is_useful_but_incomplete():
    """局部缺口不应抹掉已经可用于保守建议的专家结果。"""
    from models.contracts import AgentName, AgentResult, TaskStatus

    result = AgentResult(
        result_id="result-1", decision_id="decision-1", task_id="research",
        agent_name=AgentName.EVIDENCE_RESEARCH, completion_status="completed_with_gaps",
    )

    assert result.completion_status is TaskStatus.COMPLETED_WITH_GAPS


def test_unbound_tool_preflight_requires_a_public_reason():
    """未绑定动作若没有理由，下一轮 ReAct 无法修正参数。"""
    from models.contracts import ToolBindingAssessment

    with pytest.raises(ValidationError, match="reason"):
        ToolBindingAssessment(bound=False, reason="")


def test_complete_target_settlement_requires_every_criterion_to_be_satisfied():
    """只满足部分完成条件不能把当前信息目标提前结束。"""
    from models.contracts import TargetCriterionSettlement, TargetSettlementSubmission

    with pytest.raises(ValidationError, match="every criterion"):
        TargetSettlementSubmission(
            criteria=[
                TargetCriterionSettlement(criterion="南京天气", satisfied=True),
                TargetCriterionSettlement(criterion="苏州天气", satisfied=False, missing="缺少苏州天气"),
            ],
            coverage_status="partial",
            missing_information=["缺少苏州天气"],
            target_complete=True,
            summary="仅有南京天气资料。",
        )


def test_task_declares_structured_work_kind_and_target_limits_criteria_to_necessities():
    """路由必须依据显式工作类型，信息目标最多携带三项必要完成条件。"""
    from models.contracts import InformationTarget, TaskSpec, TaskWorkKind

    task = TaskSpec(task_id="weather", objective="任意文案", work_kind=TaskWorkKind.LOCATION_RESEARCH)
    assert task.work_kind is TaskWorkKind.LOCATION_RESEARCH

    with pytest.raises(ValidationError):
        InformationTarget(
            target_id="weather", objective="天气", completion_criteria=["a", "b", "c", "d"],
        )
