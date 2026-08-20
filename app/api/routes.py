"""提供决策、记忆、反馈与检查能力的 FastAPI 路由处理器。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.container import Services
from app.temporal_context import build_temporal_context
from app.trace_stream import encode_sse
from models.contracts import (
    ContinueRequest, DecisionListItem, DecisionRequest, DecisionResponse, DeleteDecisionsRequest, Episode,
    FeedbackRequest, FeedbackResponse, RetrospectiveRequest, WorkflowStatus,
    HITLResponse,
)


router = APIRouter()


def get_services(request: Request) -> Services:
    return request.app.state.services


ServicesDependency = Annotated[Services, Depends(get_services)]


@router.post("/decision", response_model=DecisionResponse)
async def create_decision(payload: DecisionRequest, services: ServicesDependency):
    return await services.graph.run(payload)


@router.post("/decision/stream")
async def stream_decision(payload: DecisionRequest, services: ServicesDependency):
    """用 SSE 将一次决策的可审计执行轨迹和最终报告逐步推送给浏览器。"""
    async def event_stream():
        """把图执行器产出的结构化事件编码为标准 SSE 帧。"""
        async for name, data in services.graph.stream(payload):
            yield encode_sse(name, data)

    return StreamingResponse(
        event_stream(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/decision/{decision_id}/hitl/{request_id}")
async def respond_to_hitl(decision_id: str, request_id: str, payload: HITLResponse, services: ServicesDependency):
    """将用户填写的补充字段或跳过意图交给当前等待中的决策执行。"""
    if not await services.graph.submit_hitl(decision_id, request_id, payload):
        raise HTTPException(404, "pending human-input request not found")
    return {"accepted": True, "decision_id": decision_id, "request_id": request_id}


@router.post("/decision/{decision_id}/continue", response_model=DecisionResponse)
async def continue_decision(decision_id: str, payload: ContinueRequest, services: ServicesDependency):
    if services.working.get(decision_id) is None:
        raise HTTPException(404, "decision not found")
    return await services.graph.continue_decision(decision_id, payload.instruction, payload.additional_context)


@router.get("/decision/{decision_id}", response_model=DecisionResponse)
async def get_decision(decision_id: str, services: ServicesDependency):
    record = services.archives.get(decision_id)
    if record is None:
        raise HTTPException(404, "decision not found")
    agents = [task.agent for task in record.plan.tasks] if record.plan else []
    return DecisionResponse(decision_id=record.decision_id, decision_type=record.decision_type,
        status=record.status, report=record.report, plan=record.plan,
        events=services.traces.list(decision_id), activated_agents=agents, candidates=record.candidates)


@router.get("/decisions", response_model=list[DecisionListItem])
async def list_decisions(services: ServicesDependency):
    return [DecisionListItem(
        decision_id=item.decision_id, decision_type=item.decision_type, query=item.query,
        status=item.status, recommendation=item.recommendation, confidence=item.confidence,
        created_at=item.created_at, updated_at=item.updated_at,
    ) for item in services.archives.list()]


@router.post("/decisions/delete")
async def delete_decisions(payload: DeleteDecisionsRequest, services: ServicesDependency):
    """删除前端会话对应的服务器归档，并按用户选择清理关联记忆。"""
    known = [identifier for identifier in payload.decision_ids if services.archives.get(identifier) is not None]
    if not known:
        raise HTTPException(404, "decision not found")
    episode_ids = services.memory.delete_decisions(services.archives, known, delete_memories=payload.delete_memories)
    return {"deleted_decision_ids": known, "deleted_episode_ids": episode_ids, "delete_memories": payload.delete_memories}


@router.post("/decision/{decision_id}/feedback", response_model=FeedbackResponse)
async def record_feedback(decision_id: str, payload: FeedbackRequest, services: ServicesDependency):
    archive = services.archives.get(decision_id)
    if archive is None:
        raise HTTPException(404, "decision not found")
    if payload.user_choice and archive.candidates and payload.user_choice not in archive.candidates:
        raise HTTPException(422, "user_choice must be one of the original candidates")
    services.feedback.save(decision_id, payload.user_choice, payload.outcome, payload.notes)
    episode = services.memory.episodes.by_decision_id(decision_id)
    updates: list[str] = []
    if episode is not None:
        explicit_preferences = await services.graph.judge.model_adapter.extract_explicit_profile_signals(
            decision_type=archive.decision_type, chosen_reason=payload.chosen_reason or "",
        )
        profile_texts = [reason for reason in (payload.chosen_reason, payload.not_chosen_reason) if reason and reason.strip()]
        explicit_profiles = await services.graph.judge.model_adapter.extract_user_profile_signals(
            texts=profile_texts,
            temporal_context=build_temporal_context("\n".join(profile_texts)),
        )
        inferred = f"inferred:{archive.decision_type.value}:chosen_option={payload.user_choice}" if payload.user_choice else None
        signals = list(dict.fromkeys([
            *episode.profile_signals, *([inferred] if inferred else []),
            *explicit_preferences, *explicit_profiles,
        ]))
        services.memory.write_episode(episode.model_copy(update={
            "user_choice": payload.user_choice or episode.user_choice, "outcome": payload.outcome or episode.outcome,
            "feedback": payload.notes or episode.feedback, "chosen_reason": payload.chosen_reason or episode.chosen_reason,
            "not_chosen_reason": payload.not_chosen_reason or episode.not_chosen_reason,
            "profile_signals": signals,
        }))
        updates = [*([inferred] if inferred else []), *explicit_preferences, *explicit_profiles]
    return FeedbackResponse(decision_id=decision_id, accepted=True, profile_updates=updates)


@router.post("/decision/{decision_id}/retrospective")
async def retrospective(decision_id: str, services: ServicesDependency, payload: RetrospectiveRequest | None = None):
    archive = services.archives.get(decision_id)
    if archive is None:
        raise HTTPException(404, "decision not found")
    recorded = services.feedback.list(decision_id)
    latest = recorded[0] if recorded else None
    outcome = (payload.outcome if payload else None) or (latest.outcome if latest else None)
    user_choice = latest.user_choice if latest else None
    correct = ["推荐与用户实际选择一致"] if user_choice and user_choice == archive.recommendation else []
    incorrect = ["推荐与用户实际选择不同，需要检查偏好权重"] if user_choice and user_choice != archive.recommendation else []
    missing = list(archive.report.uncertainties if archive.report else ["原报告不可用"])
    result = {
        "decision_id": decision_id, "correct_items": correct, "incorrect_items": incorrect,
        "missing_information": missing, "wrong_assumptions": list(archive.report.risks if archive.report else []),
        "preference_updates": [f"实际选择：{user_choice}"] if user_choice else [],
        "future_lessons": ["下次决策应优先核验本次未确认的信息", "将实际结果与原始硬约束逐项对照"],
        "outcome": outcome,
    }
    services.retrospectives.save(decision_id, result)
    episode = services.memory.episodes.by_decision_id(decision_id)
    if episode is not None:
        services.memory.write_episode(episode.model_copy(update={
            "outcome": outcome or episode.outcome,
            "feedback": (latest.notes if latest else None) or episode.feedback,
            "profile_signals": list(dict.fromkeys([*episode.profile_signals, *([f"inferred:{archive.decision_type.value}:chosen_option={user_choice}"] if user_choice else [])])),
        }))
    return result


@router.get("/memory/profile")
async def profile_memory(services: ServicesDependency):
    return [item.model_dump(mode="json") for item in services.memory.profiles.list()]


@router.get("/memory/episodes")
async def episode_memory(services: ServicesDependency):
    return [item.model_dump(mode="json") for item in services.memory.episodes.list()]


@router.get("/skills")
async def list_skills(services: ServicesDependency):
    return [item.to_dict() for item in services.skills.list()]


@router.get("/mcp/tools")
async def list_mcp_tools(services: ServicesDependency):
    return [item.model_dump(mode="json") for item in services.gateway.registry.list_capabilities()]
