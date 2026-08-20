"""根据证据重要性决定是否进行第二来源验证的服务。"""

from __future__ import annotations

from uuid import uuid4

from app.trace_stream import TraceSink, emit_to_sink
from models.contracts import AgentName, Evidence, EvidenceStatus, ToolCallStatus
from .pool import EvidencePool


class EvidenceVerifier:
    """对重要或冲突证据发起第二来源核验，并记录核验轨迹。"""

    def __init__(self, gateway, pool: EvidencePool, trace_sink: TraceSink | None = None) -> None:
        self.gateway, self.pool, self.trace_sink = gateway, pool, trace_sink

    async def verify(self, evidence_id: str, *, material: bool = True) -> Evidence:
        item = self.pool.get(evidence_id)
        if item is None:
            raise ValueError(f"unknown evidence ID: {evidence_id}")
        same_claim_sources = {entry.source for entry in self.pool.list() if entry.claim == item.claim and entry.source}
        needs_second_source = material and (len(same_claim_sources) < 2 or item.status == EvidenceStatus.CONFLICTING)
        if not needs_second_source:
            return item
        await emit_to_sink(
            self.trace_sink, "verification_action", "开始第二来源证据核验",
            "该证据重要、来源不足或存在冲突，需要补充独立信息进行验证。",
            {"evidence_id": item.evidence_id, "claim": item.claim, "source": item.source},
        )
        # 不假定所有搜索工具都接受来源排除字段，只发送其共同要求的查询参数。
        arguments = {"query": item.claim}
        tool_name = self.gateway.select_tool_name(AgentName.EVIDENCE_RESEARCH, "web_search", arguments)
        if tool_name is None:
            observation = ToolObservation(
                call_id=str(uuid4()), decision_id=item.decision_id, task_id="verify",
                agent=AgentName.EVIDENCE_RESEARCH, tool_name="web_search", arguments=arguments,
                status=ToolCallStatus.UNAVAILABLE,
                error="没有可接收当前参数的明确 web_search 工具，无法进行第二来源核验。",
            )
        else:
            observation = await self.gateway.call_tool(
                AgentName.EVIDENCE_RESEARCH, tool_name, arguments,
                decision_id=item.decision_id, task_id="verify", trace_sink=self.trace_sink,
            )
        await emit_to_sink(
            self.trace_sink, "verification_observation", "证据核验结果",
            observation.result_summary or observation.error or "第二来源未返回可用信息。",
            {"evidence_id": item.evidence_id, "tool": observation.tool_name,
             "status": observation.status.value, "latency_ms": observation.latency_ms,
             "result_summary": observation.result_summary, "error": observation.error},
        )
        if observation.status == ToolCallStatus.SUCCEEDED and observation.result_summary:
            corroboration = Evidence(
                evidence_id=f"EV-{uuid4().hex[:12]}", decision_id=item.decision_id,
                claim=item.claim, value=item.value, source=f"verification:{observation.tool_name}",
                source_type="second_source", agent=AgentName.EVIDENCE_RESEARCH,
                tool=observation.tool_name, confidence=min(1.0, max(item.confidence, .7)),
                status=EvidenceStatus.CONFIRMED, supports=[item.evidence_id],
            )
            item.status = EvidenceStatus.CONFIRMED
            item.supports = list(dict.fromkeys([*item.supports, corroboration.evidence_id]))
            self.pool.add(corroboration); self.pool.save(item)
        elif observation.status in {
            ToolCallStatus.UNAVAILABLE, ToolCallStatus.TIMED_OUT,
            ToolCallStatus.FAILED, ToolCallStatus.DENIED,
        }:
            if item.status != EvidenceStatus.CONFLICTING:
                item.status = EvidenceStatus.UNVERIFIED
            self.pool.save(item)
        return item
