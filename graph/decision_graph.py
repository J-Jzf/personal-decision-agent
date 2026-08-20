"""具备持久化状态迁移和有限重规划能力的 Plan-and-Execute 工作流。"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, AsyncIterator
from uuid import uuid4

from agents.base import AgentContext
from app.trace_stream import TracePublisher, sanitize_trace_value
from app.temporal_context import merge_temporal_context
from agents.debate import DebateArgument, DebateModerator
from agents.evidence_research import EvidenceResearchAgent
from agents.financial_market import FinancialMarketAgent
from agents.general import GeneralAgent
from agents.location_lifestyle import LocationLifestyleAgent
from agents.preference import PreferenceAgent
from agents.risk_critic import RiskCritic
from evidence.pool import EvidencePool
from evidence.verifier import EvidenceVerifier
from models.contracts import (
    AgentName, DecisionRequest, DecisionResponse, Episode, Evidence,
    EvidenceStatus, ExecutionPlan, HITLRequest, HITLResponse, HITLStatus, TaskSpec, TaskStatus,
    ToolCallStatus, WorkflowEvent, WorkflowStatus,
)
from .routing import route_after_execution, should_debate
from .states import DecisionState


class DecisionGraph:
    def __init__(self, *, planner, judge, memory, skills, gateway,
                 archives, working, evidence_repository, agent_results_repository,
                 traces) -> None:
        self.planner, self.judge = planner, judge
        self.memory, self.skills, self.gateway = memory, skills, gateway
        self.archives, self.working = archives, working
        self.evidence_repository = evidence_repository
        self.agent_results_repository, self.traces = agent_results_repository, traces
        self.agents = {
            AgentName.EVIDENCE_RESEARCH: EvidenceResearchAgent(),
            AgentName.FINANCIAL_MARKET: FinancialMarketAgent(),
            AgentName.LOCATION_LIFESTYLE: LocationLifestyleAgent(),
            AgentName.PREFERENCE: PreferenceAgent(),
            AgentName.RISK_CRITIC: RiskCritic(),
            AgentName.GENERAL: GeneralAgent(),
        }
        self._hitl_waiters: dict[tuple[str, str], asyncio.Future[HITLResponse]] = {}

    async def run(self, request: DecisionRequest) -> DecisionResponse:
        return await self._run(request, str(uuid4()), continued=False)

    async def stream(self, request: DecisionRequest) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        """在后台执行决策，并按发生顺序产出可审计的实时轨迹和最终结果。"""
        decision_id = str(uuid4())
        publisher = TracePublisher(decision_id, self.traces)
        await publisher.emit(
            state=WorkflowStatus.RECEIVED, kind="decision_started", title="开始分析决策",
            summary="已接收你的问题，正在识别任务并准备决策流程。",
            payload={"decision_id": decision_id},
        )

        async def execute_in_background() -> None:
            """即使浏览器中断读取，也继续完成并持久化本次决策。"""
            try:
                publisher.result = await self._run(request, decision_id, continued=False, publisher=publisher)
            except Exception as error:
                publisher.error = str(error)
                await publisher.emit(
                    state=WorkflowStatus.FAILED, kind="decision_failed", title="决策执行失败",
                    summary="执行过程中出现未恢复的错误，已保留此前完成的轨迹。",
                    payload={"error": str(error)},
                )
            finally:
                await publisher.close()

        asyncio.create_task(execute_in_background())
        while True:
            event = await publisher.next_event()
            if event is None:
                break
            yield event.kind, event.model_dump(mode="json")
        if publisher.error is not None:
            yield "error", {"decision_id": decision_id, "error": sanitize_trace_value(publisher.error)}
        elif publisher.result is not None:
            yield "decision_completed", {
                "decision_id": decision_id,
                "response": publisher.result.model_dump(mode="json"),
            }

    async def continue_decision(self, decision_id: str, instruction: str = "继续", additional_context: dict[str, Any] | None = None) -> DecisionResponse:
        saved = self.working.get(decision_id)
        if saved is None:
            raise KeyError(decision_id)
        prior = DecisionState.model_validate(saved.state)
        request = prior.request.model_copy(update={
            "query": f"{prior.request.query}\n继续说明：{instruction}",
            "context": {**prior.request.context, **(additional_context or {})},
        })
        return await self._run(request, decision_id, continued=True, prior=prior)

    async def submit_hitl(self, decision_id: str, request_id: str, response: HITLResponse) -> bool:
        """将浏览器的补充信息交给仍在等待的工作流；过期请求不能覆盖后续状态。"""
        waiter = self._hitl_waiters.get((decision_id, request_id))
        if waiter is None or waiter.done():
            return False
        waiter.set_result(response)
        return True

    async def _run(self, request: DecisionRequest, decision_id: str, *, continued: bool,
                   prior: DecisionState | None = None,
                   publisher: TracePublisher | None = None) -> DecisionResponse:
        self.gateway.reset_decision_budget(decision_id)
        self.judge.model_adapter.reset_fallback_events()
        state = DecisionState(decision_id=decision_id, request=request,
                              checkpoint_version=prior.checkpoint_version if prior else 0)
        state.request = self._with_user_texts(state.request, [state.request.query])
        pool = EvidencePool(self.evidence_repository, decision_id)
        await self._transition(state, WorkflowStatus.RECEIVED, {"continued": continued}, publisher)
        initial_memory = self.memory.context_for_any(state.request.query)
        autonomous = await self.planner.create_autonomous_plan(
            state.request, skills=self.skills.list(), tools=self.gateway.registry.list_capabilities(), memory=initial_memory,
        )
        for event in [item for item in self.judge.model_adapter.fallback_events if item.get("mode") == "structured_retry"]:
            await self._event(state, "model_output_retry", "总控结构化输出正在修正",
                              f"总控输出未通过合同校验，正在进行第 {event['attempt']} 次修正。",
                              {"operation": event["operation"], "attempt": event["attempt"], "validation_error": event["reason"]}, publisher)
        if autonomous.hitl_question and autonomous.hitl_rationale:
            planning_hitl = HITLRequest(
                request_id=str(uuid4()), decision_id=decision_id, source_agent=AgentName.PLANNER,
                stage="planning", question=autonomous.hitl_question, rationale=autonomous.hitl_rationale,
                fields=autonomous.hitl_fields,
            )
            resolved = await self._wait_for_human_input(state, planning_hitl, publisher)
            values = {**resolved.response_values}
            if resolved.free_text:
                values["free_text"] = resolved.free_text
            state.request = self._with_user_texts(
                state.request, self._hitl_texts(values),
                extra_context={"hitl": values},
            )
            autonomous = await self.planner.create_autonomous_plan(
                state.request, skills=self.skills.list(), tools=self.gateway.registry.list_capabilities(), memory=initial_memory,
            )
        state.decision_type = autonomous.decision_type
        has_deterministic_fallback = any(
            event.get("mode") == "deterministic_fallback"
            for event in self.judge.model_adapter.fallback_events
        )
        await self._transition(
            state, WorkflowStatus.CLASSIFIED,
            {"decision_type": state.decision_type.value, "mode": "deterministic_fallback" if has_deterministic_fallback else "model"},
            publisher,
        )
        state.memory = self.memory.context_for(state.request.query, state.decision_type)
        await self._transition(state, WorkflowStatus.MEMORY_RETRIEVED, {"episodes": len(state.memory.episodes), "profiles": len(state.memory.profile_memories)}, publisher)
        skill = self.skills.get(autonomous.skill_name) if autonomous.skill_name else self.skills.get("risk-debate-moderator")
        state.skill_name = autonomous.skill_name
        await self._transition(state, WorkflowStatus.SKILL_LOADED, {"skill": skill.name}, publisher)
        state.plan = autonomous.plan
        self._register_tasks(state, state.plan.tasks)
        state.activated_agents = [task.agent for task in state.plan.tasks]
        await self._transition(state, WorkflowStatus.PLANNED, {"tasks": len(state.plan.tasks), "agents": [item.value for item in state.activated_agents]}, publisher)
        await self._event(state, "plan_created", "已制定执行计划", autonomous.planning_summary, {"plan": state.plan.model_dump(mode="json"), "skill": state.skill_name}, publisher)
        while True:
            await self._execute(state, pool, publisher)
            route = await route_after_execution(state, pool, self.judge.model_adapter)
            if route != "replan" or state.replan_count >= self.judge.model_adapter.settings.replan_limit:
                break
            state.replan_count += 1
            replan_decision = state.replan_decision
            await self._transition(
                state, WorkflowStatus.REPLANNING,
                {"reason": replan_decision.reason if replan_decision else "总控认为存在可补救的关键缺口",
                 "critical_gaps": replan_decision.critical_gaps if replan_decision else [], "attempt": state.replan_count},
                publisher,
            )
            replacement = await self.planner.create_autonomous_plan(
                state.request, skills=self.skills.list(), tools=self.gateway.registry.list_capabilities(), memory=state.memory,
                execution_context=self._replan_context(state, pool),
            )
            remaining_tasks, skipped_tasks = self.incremental_replan_tasks(state, replacement.plan)
            unmet_gaps = self._replan_context(state, pool)["unmet_gaps"]
            if not self.has_executable_replan_tasks(remaining_tasks, unmet_gaps):
                state.plan = state.plan.model_copy(update={"tasks": []}) if state.plan else replacement.plan
                state.activated_agents = []
                await self._event(
                    state, "replan_no_executable_tasks", "重规划未产生可执行补充任务",
                    "总控已知晓尚未满足的证据缺口，但替代计划没有给出可执行任务；将保留缺口并进入核验或最终判断。",
                    {"attempt": state.replan_count, "unmet_gaps": unmet_gaps,
                     "proposed_plan": replacement.plan.model_dump(mode="json")}, publisher,
                )
                break
            self._register_tasks(state, replacement.plan.tasks)
            state.plan = replacement.plan.model_copy(update={"tasks": remaining_tasks})
            state.activated_agents = [task.agent for task in state.plan.tasks]
            await self._event(state, "plan_replanned", "已生成替代执行计划", replacement.planning_summary,
                              {"plan": state.plan.model_dump(mode="json"), "attempt": state.replan_count,
                               "reused_completed_task_ids": skipped_tasks}, publisher)
            if skipped_tasks:
                await self._event(
                    state, "replan_reused_work", "重规划复用了已有执行结果",
                    "已保留此前完成任务和成功证据，仅从未满足的缺口继续执行。",
                    {"reused_completed_task_ids": skipped_tasks}, publisher,
                )
        if state.plan.requires_verification or pool.conflicts():
            await self._transition(state, WorkflowStatus.VERIFYING, {"conflicts": len(pool.conflicts())}, publisher)
            async def verification_trace(kind: str, title: str, summary: str, payload: dict[str, Any]):
                """将证据核验的工具活动纳入当前请求的实时轨迹。"""
                return await self._event(state, kind, title, summary, payload, publisher)

            verifier = EvidenceVerifier(self.gateway, pool, verification_trace)
            for item in list(pool.list()):
                if item.status in {EvidenceStatus.CONFLICTING, EvidenceStatus.PENDING} and item.confidence >= .6:
                    await verifier.verify(item.evidence_id, material=True)
        if should_debate(state, pool):
            await self._transition(state, WorkflowStatus.DEBATING, {"reason": "重要争议或 Skill 要求"}, publisher)
            pro = [DebateArgument(side="pro", text=f"支持：{item.claim}", evidence_ids=[item.evidence_id]) for item in pool.list() if item.status == EvidenceStatus.CONFIRMED]
            con = [DebateArgument(side="con", text=f"反对：{item.claim}", evidence_ids=[item.evidence_id]) for item in pool.conflicts()]
            if pro or con:
                debate = DebateModerator(pool).run(pro, con)
                await self._checkpoint(state, {"debate": debate.model_dump(mode="json")}, publisher)
        await self._transition(state, WorkflowStatus.JUDGING, {}, publisher)
        state.report = await self.judge.decide(
            state.request, evidence_pool=pool, memory=state.memory, dimensions=skill.analysis_dimensions,
            execution_context=self._controller_execution_context(state, pool),
        )
        fallback_events = getattr(self.judge.model_adapter, "fallback_events", [])
        state.fallback_events = list(fallback_events)
        await self._transition(state, WorkflowStatus.COMPLETED, {"fallback_events": state.fallback_events}, publisher)
        self.archives.save(
            decision_id=decision_id, decision_type=state.decision_type, query=state.request.query,
            status=WorkflowStatus.ARCHIVED, candidates=request.candidates,
            constraints=state.request.constraints, preferences=state.request.preferences,
            plan=state.plan, report=state.report,
            recommendation=state.report.recommended_option, confidence=state.report.confidence,
        )
        profile_signals = await self.judge.model_adapter.extract_user_profile_signals(
            texts=self._profile_source_texts(state.request),
            temporal_context=self._temporal_context(state.request),
        )
        self.memory.archive_completed(Episode(
            episode_id=str(uuid4()), decision_id=decision_id, decision_type=state.decision_type,
            summary=state.request.query, options=state.request.candidates,
            constraints=state.request.constraints, preferences=state.request.preferences,
            tags=[state.decision_type.value, state.skill_name or "general"],
            profile_signals=profile_signals,
        ))
        if profile_signals:
            await self._event(
                state, "profile_memory_updated", "已更新用户画像记忆",
                "已从你亲自提供的信息中提炼长期画像，后续相关决策会参考它。",
                {"profile_keys": [signal.split(":", 2)[2].split("=", 1)[0] for signal in profile_signals]},
                publisher,
            )
        await self._transition(state, WorkflowStatus.ARCHIVED, {"recommendation": state.report.recommended_option}, publisher)
        await self._event(state, "final_report", "已形成最终建议", "已汇总证据、偏好、风险与不确定性，生成可执行的决策报告。", {"recommendation": state.report.recommended_option, "confidence": state.report.confidence}, publisher)
        return self._response(state)

    async def _execute(self, state: DecisionState, pool: EvidencePool,
                       publisher: TracePublisher | None = None) -> None:
        await self._transition(state, WorkflowStatus.EXECUTING, {}, publisher)
        if state.plan is None:
            raise RuntimeError("cannot execute without a plan")
        completed: set[str] = set()
        for task in state.plan.tasks:
            if not set(task.dependencies) <= completed:
                missing_dependencies = sorted(set(task.dependencies) - completed)
                self._set_task_status(state, task, TaskStatus.SKIPPED.value)
                await self._event(
                    state, "task_dependency_unsatisfied", "任务依赖尚未满足",
                    "上游任务未完整完成；本任务不会被误执行，将交给总控重规划或最终判断。",
                    {"task_id": task.task_id, "dependencies": missing_dependencies}, publisher,
                )
                continue
            agent = self.agents[task.agent]
            self._set_task_status(state, task, "in_progress")

            async def trace_sink(kind: str, title: str, summary: str, payload: dict[str, Any]):
                """把专家和网关事件绑定到当前决策状态与当前 SSE 发布器。"""
                return await self._event(state, kind, title, summary, payload, publisher)

            await self._event(
                state, "agent_task_started", "专家 Agent 开始执行任务",
                f"{task.agent.value} 正在处理：{task.objective}",
                {"task": task.model_dump(mode="json"), "agent": task.agent.value}, publisher,
            )
            # 每个专家拿到同一决策的只读快照；执行后再把局部观察、覆盖和结果回写状态机。
            context = AgentContext(
                decision_id=state.decision_id, gateway=self.gateway, memory=state.memory,
                evidence_pool=pool, request_context={"constraints": state.request.constraints, **state.request.context}, trace_sink=trace_sink,
                model_adapter=self.judge.model_adapter, request=state.request,
                available_tools=[item for item in self.gateway.registry.list_capabilities() if task.agent in item.allowed_agents],
                execution_context=self._react_execution_context(state, task, pool),
                information_coverage=state.information_coverage,
                information_targets=state.information_targets.get(task.task_id, []),
            )

            async def specialist_delegate(parent_task: TaskSpec, delegation, parent_context: AgentContext) -> dict[str, Any]:
                """General 的事实委派：运行真实专家，但不把 MCP 直接暴露给 General。"""
                factual_agents = {
                    AgentName.EVIDENCE_RESEARCH, AgentName.FINANCIAL_MARKET, AgentName.LOCATION_LIFESTYLE,
                }
                if delegation.agent not in factual_agents:
                    raise ValueError("general 只能委派给事实检索专家")
                ordinal = len(parent_context.delegated_results) + 1
                child_task = TaskSpec(
                    task_id=f"{parent_task.task_id}.delegate.{delegation.agent.value}.{ordinal}",
                    objective=delegation.objective, agent=delegation.agent, work_kind=delegation.work_kind,
                    required_capabilities=delegation.required_capabilities,
                    completion_criteria=delegation.completion_criteria,
                )
                await self._event(
                    state, "general_delegation_started", "通用 Agent 委派事实专家",
                    f"general 正在委派 {delegation.agent.value} 补齐必要外部事实。",
                    {"parent_task_id": parent_task.task_id, "delegated_task": child_task.model_dump(mode="json")}, publisher,
                )
                child_context = AgentContext(
                    decision_id=state.decision_id, gateway=self.gateway, memory=state.memory,
                    evidence_pool=pool, request_context=context.request_context, trace_sink=trace_sink,
                    model_adapter=self.judge.model_adapter, request=state.request,
                    available_tools=[item for item in self.gateway.registry.list_capabilities() if delegation.agent in item.allowed_agents],
                    execution_context=self._react_execution_context(state, child_task, pool),
                    information_coverage=state.information_coverage,
                )
                result = await self.agents[delegation.agent].execute(child_task, child_context)
                state.information_targets[child_task.task_id] = child_context.information_targets
                parent_context.observations.extend(child_context.observations)
                parent_context.information_coverage = child_context.information_coverage
                parent_context.execution_context.setdefault("delegated_task_specs", {})[child_task.task_id] = child_task.model_dump(mode="json")
                await self._event(
                    state, "general_delegation_completed", "事实专家委派完成", result.summary,
                    {"parent_task_id": parent_task.task_id, "delegated_task_id": child_task.task_id,
                     "agent": delegation.agent.value, "findings": result.findings,
                     "uncertainties": result.uncertainties, "completion_status": result.completion_status.value}, publisher,
                )
                return result.model_dump(mode="json")

            if task.agent == AgentName.GENERAL:
                context.specialist_delegate = specialist_delegate
            async def human_input_handler(hitl: HITLRequest) -> None:
                """暂停当前专家并把可选字段、跳过或超时结果写回同一决策上下文。"""
                resolved = await self._wait_for_human_input(state, hitl, publisher)
                values = {**resolved.response_values}
                if resolved.free_text:
                    values["free_text"] = resolved.free_text
                state.request = self._with_user_texts(
                    state.request, self._hitl_texts(values),
                    extra_context={"hitl": values},
                )
                context.request = state.request

            context.human_input_handler = human_input_handler
            result = await agent.execute(task, context)
            # 先汇合专家局部状态，再从“直接支持本目标”的观察生成正式 Evidence，防止交叉资料被错误归属。
            state.agent_results.append(result); self.agent_results_repository.save(result)
            state.information_coverage = context.information_coverage
            state.information_targets[task.task_id] = context.information_targets
            self._set_task_status(state, task, result.completion_status.value, result)
            state.tool_observations.extend(context.observations)
            for observation in context.observations:
                if self._observation_is_usable_evidence(observation):
                    await self._record_observation_evidence(state, pool, task, observation)
            completed = self.satisfied_task_ids(state.agent_results)
            await self._event(
                state, "agent_task_completed", "专家 Agent 完成任务", result.summary,
                {"task_id": task.task_id, "agent": task.agent.value,
                 "completion_status": result.completion_status.value, "findings": result.findings,
                 "uncertainties": result.uncertainties, "tool_calls_used": result.tool_calls_used}, publisher,
            )
            if task.agent == AgentName.RISK_CRITIC:
                await self._event(
                    state, "risk_review_completed", "风险专家审查结果", result.summary,
                    {"task_id": task.task_id, "agent": task.agent.value,
                     "findings": result.findings, "uncertainties": result.uncertainties,
                     "requires_replan": bool(result.uncertainties)}, publisher,
                )
            progress = self.build_progress_summary(state, pool, result)
            state.progress_summaries.append(progress)
            await self._event(
                state, "controller_progress_summary", "总控进度摘要",
                str(progress["下一步应该做"]), {"progress_summary": progress}, publisher,
            )
            await self._checkpoint(
                state, {"task_id": task.task_id, "completion_status": result.completion_status.value,
                        "progress_summary": progress}, publisher,
            )

    @staticmethod
    def _register_tasks(state: DecisionState, tasks: list[TaskSpec]) -> None:
        """将每版计划登记进任务账本，重规划时保留已执行任务的结构化状态。"""
        for task in tasks:
            previous = state.task_ledger.get(task.task_id, {})
            state.task_ledger[task.task_id] = {
                **previous,
                "task_id": task.task_id,
                "objective": task.objective,
                "agent": task.agent.value,
                "dependencies": list(task.dependencies),
                "completion_criteria": list(task.completion_criteria),
                "status": previous.get("status", "pending"),
            }

    @staticmethod
    def _set_task_status(state: DecisionState, task: TaskSpec, status: str, result: Any | None = None) -> None:
        """更新任务账本状态，并把专家已经获得的发现与不确定性一并保存。"""
        DecisionGraph._register_tasks(state, [task])
        entry = state.task_ledger[task.task_id]
        entry["status"] = status
        if result is not None:
            entry["findings"] = list(result.findings)
            entry["uncertainties"] = list(result.uncertainties)
            entry["tool_calls_used"] = result.tool_calls_used

    @staticmethod
    def satisfied_task_ids(results: list[Any]) -> set[str]:
        """完整或带缺口完成的任务都可解锁普通下游综合；真正 blocked 仍不可用。"""
        return {
            result.task_id for result in results
            if getattr(result, "completion_status", None) in {
                TaskStatus.COMPLETED, TaskStatus.COMPLETED_WITH_GAPS,
            }
        }

    @staticmethod
    def _evidence_scope(task: TaskSpec, observation) -> str:
        """以任务、信息目标、工具和规范化参数形成可比较范围，避免把不同资料混为同一主张。"""
        arguments = json.dumps(observation.arguments, ensure_ascii=False, sort_keys=True, default=str)
        return f"{task.task_id}|{observation.target_id or 'task'}|{observation.tool_name}|{arguments}"

    @staticmethod
    def _evidence_claim(task: TaskSpec, state: DecisionState, target_id: str | None) -> str:
        """优先使用信息目标说明作为事实主张；缺少归属时才回退到整个专家任务。"""
        for target in state.information_targets.get(task.task_id, []):
            if target.get("target_id") == target_id and isinstance(target.get("objective"), str):
                return target["objective"]
        return task.objective

    async def _record_observation_evidence(self, state: DecisionState, pool: EvidencePool,
                                           task: TaskSpec, observation) -> None:
        """保存成功观察，并只对同一结构化范围的不同资料请求模型关系判断。"""
        scope_key = self._evidence_scope(task, observation)
        candidate = Evidence(
            evidence_id=f"EV-{uuid4().hex[:12]}", decision_id=state.decision_id,
            claim=self._evidence_claim(task, state, observation.target_id), scope_key=scope_key,
            value=observation.result_summary, source=f"mcp:{observation.tool_name}", source_type="external_tool",
            agent=observation.agent, tool=observation.tool_name, confidence=.65,
            status=EvidenceStatus.UNVERIFIED,
        )
        related = pool.in_scope(scope_key)
        stored = pool.add(candidate)
        if stored.evidence_id != candidate.evidence_id:
            return
        for existing in related:
            relationship = await self.judge.model_adapter.evidence_relationship_or_fallback(
                left=existing, right=candidate,
            )
            pool.apply_relation(existing, candidate, relationship.relation)
            await self._event(
                state, "evidence_relationship_assessed", "已判断同范围证据关系",
                relationship.summary,
                {"scope": scope_key, "left_evidence_id": existing.evidence_id,
                 "right_evidence_id": candidate.evidence_id, "relation": relationship.relation},
            )

    @staticmethod
    def _all_successful_information(state: DecisionState) -> list[dict[str, Any]]:
        """汇总所有计划版本的成功观察和专家发现，供后续任务与重规划复用。"""
        information = [
            {
                "task_id": observation.task_id,
                "agent": observation.agent.value,
                "tool_name": observation.tool_name,
                "arguments": observation.arguments,
                "result_summary": observation.result_summary,
            }
            for observation in state.tool_observations
            if DecisionGraph._observation_is_usable_evidence(observation)
        ]
        for task_id, entry in state.task_ledger.items():
            for finding in entry.get("findings", []):
                information.append({
                    "task_id": task_id,
                    "agent": entry.get("agent"),
                    "result_summary": finding,
                })
        return information

    @staticmethod
    def _structured_coverage(state: DecisionState) -> dict[str, Any]:
        """按工具参数通用地记录已覆盖实体，避免跨任务遗忘已取得的资料。"""
        coverage: dict[str, Any] = {}
        for observation in state.tool_observations:
            if not DecisionGraph._observation_is_usable_evidence(observation):
                continue
            domain = re.sub(r"(?:[_-](?:old|new|retry|replan|\d+))+$", "", observation.task_id)
            entry = coverage.setdefault(domain, {"covered_input_values": {}})
            for key, value in observation.arguments.items():
                if isinstance(value, str) and value.strip():
                    values = entry["covered_input_values"].setdefault(key, [])
                    if value not in values:
                        values.append(value)
        for entry in coverage.values():
            locations = entry["covered_input_values"].get("location", [])
            if locations:
                entry["covered_locations"] = locations
                entry["missing_locations"] = [item for item in state.request.candidates if item not in locations]
        return coverage

    @staticmethod
    def _observation_is_usable_evidence(observation) -> bool:
        """仅将通过语义核验的新增观察或兼容的历史观察写入证据与跨任务上下文。"""
        return (
            getattr(observation, "supports_current_target", True)
            and DecisionGraph._observation_is_referenceable(observation)
        )

    @staticmethod
    def _observation_is_referenceable(observation) -> bool:
        """保留可支持同一决策其他目标的部分资料，供后续目标按 call_id 引用。"""
        return (
            observation.status == ToolCallStatus.SUCCEEDED and bool(observation.result_summary)
            and getattr(observation, "semantic_status", None) in {None, "relevant", "partial"}
        )

    @staticmethod
    def _observation_target_is_unresolved(state: DecisionState, observation) -> bool:
        """已 complete target 的历史失败只保留审计轨迹，不再伪装成总控缺口。"""
        if not observation.target_id:
            return True
        for target in state.information_targets.get(observation.task_id, []):
            if target.get("target_id") == observation.target_id:
                return target.get("status") != "complete"
        return True

    @staticmethod
    def incremental_replan_tasks(state: DecisionState, replacement: ExecutionPlan) -> tuple[list[TaskSpec], list[str]]:
        """去除已完成任务及其依赖，确保重规划只执行尚未满足的分析缺口。"""
        # completed_with_gaps 已有可综合资料，重规划不能把它当成完全未执行而反复运行。
        completed_ids = {
            task_id for task_id, entry in state.task_ledger.items()
            if entry.get("status") in {TaskStatus.COMPLETED.value, TaskStatus.COMPLETED_WITH_GAPS.value}
        } | {
            result.task_id for result in state.agent_results
            if result.completion_status in {TaskStatus.COMPLETED, TaskStatus.COMPLETED_WITH_GAPS}
        }
        remaining: list[TaskSpec] = []
        skipped: list[str] = []
        for task in replacement.tasks:
            if task.task_id in completed_ids:
                skipped.append(task.task_id)
                continue
            remaining.append(task.model_copy(update={
                "dependencies": [dependency for dependency in task.dependencies if dependency not in completed_ids],
            }))
        return remaining, skipped

    @staticmethod
    def _replan_context(state: DecisionState, pool: EvidencePool) -> dict[str, Any]:
        """为总控提供已完成工作和失败原因，使其增量生成替代计划。"""
        unmet_gaps = list(state.plan.missing_information) if state.plan else []
        for result in state.agent_results:
            if result.completion_status != TaskStatus.COMPLETED:
                unmet_gaps.extend(result.uncertainties or [f"任务 {result.task_id} 未完成"])
        for observation in state.tool_observations:
            if observation.status != ToolCallStatus.SUCCEEDED and observation.error and DecisionGraph._observation_target_is_unresolved(state, observation):
                unmet_gaps.append(observation.error)
        # 证据账本保留可引用的跨目标资料；正式 Evidence 仍只收录直接支撑其所属目标的观察。
        return {
            "completed_tasks": [
                {
                    "task_id": result.task_id, "agent": result.agent_name.value,
                    "completion_status": result.completion_status.value,
                    "findings": result.findings, "uncertainties": result.uncertainties,
                }
                for result in state.agent_results
                if result.completion_status in {TaskStatus.COMPLETED, TaskStatus.COMPLETED_WITH_GAPS}
            ],
            "successful_evidence": [
                {
                    "evidence_id": evidence.evidence_id, "claim": evidence.claim,
                    "value": evidence.value, "source": evidence.source,
                    "tool": evidence.tool, "status": evidence.status.value,
                }
                for evidence in pool.list()
                if evidence.status in {EvidenceStatus.CONFIRMED, EvidenceStatus.UNVERIFIED}
            ],
            "failed_tools": [
                {
                    "tool_name": observation.tool_name, "task_id": observation.task_id,
                    "arguments": observation.arguments, "status": observation.status.value,
                    "error": observation.error,
                    "semantic_status": observation.semantic_status.value if observation.semantic_status else None,
                    "semantic_summary": observation.semantic_summary,
                }
                for observation in state.tool_observations
                if not DecisionGraph._observation_is_referenceable(observation)
                and DecisionGraph._observation_target_is_unresolved(state, observation)
            ],
            "unmet_gaps": list(dict.fromkeys(item for item in unmet_gaps if item)),
            "task_statuses": list(state.task_ledger.values()),
            "all_successful_information": DecisionGraph._all_successful_information(state),
            "structured_coverage": DecisionGraph._structured_coverage(state),
            "information_coverage": state.information_coverage,
            "information_targets": state.information_targets,
            "evidence_ledger": [
                observation.model_dump(mode="json") for observation in state.tool_observations
                if DecisionGraph._observation_is_referenceable(observation)
            ],
            "latest_progress_summary": state.progress_summaries[-1] if state.progress_summaries else {},
        }

    @staticmethod
    def _react_execution_context(state: DecisionState, task: TaskSpec,
                                 pool: EvidencePool) -> dict[str, Any]:
        """给每轮专家决策提供跨任务进度，避免遗忘已完成工作或重复已失败调用。"""
        # 此集合同时驱动依赖解锁和 Prompt 中的“已完成任务”，必须与任务状态语义保持一致。
        completed_ids = {
            task_id for task_id, entry in state.task_ledger.items()
            if entry.get("status") in {TaskStatus.COMPLETED.value, TaskStatus.COMPLETED_WITH_GAPS.value}
        } | {
            result.task_id for result in state.agent_results
            if result.completion_status in {TaskStatus.COMPLETED, TaskStatus.COMPLETED_WITH_GAPS}
        }
        return {
            "user_question": state.request.query,
            "all_tasks": list(state.task_ledger.values()) or [item.model_dump(mode="json") for item in (state.plan.tasks if state.plan else [])],
            "completed_tasks": [
                {
                    "task_id": result.task_id, "agent": result.agent_name.value,
                    "completion_status": result.completion_status.value,
                    "findings": result.findings, "uncertainties": result.uncertainties,
                }
                for result in state.agent_results if result.task_id in completed_ids
            ],
            "current_task": task.model_dump(mode="json"),
            "current_task_history": [
                observation.model_dump(mode="json") for observation in state.tool_observations
                if observation.task_id == task.task_id
            ],
            "all_successful_information": DecisionGraph._all_successful_information(state),
            "structured_coverage": DecisionGraph._structured_coverage(state),
            "information_coverage": state.information_coverage,
            "information_targets": state.information_targets,
            "task_statuses": list(state.task_ledger.values()),
            "successful_evidence": [
                {
                    "claim": evidence.claim, "value": evidence.value,
                    "source": evidence.source, "tool": evidence.tool,
                }
                for evidence in pool.list()
                if evidence.status in {EvidenceStatus.CONFIRMED, EvidenceStatus.UNVERIFIED}
            ],
            "evidence_ledger": [
                observation.model_dump(mode="json") for observation in state.tool_observations
                if DecisionGraph._observation_is_referenceable(observation)
            ],
            "failed_tools": [
                {
                    "tool_name": observation.tool_name, "arguments": observation.arguments,
                    "status": observation.status.value, "error": observation.error,
                }
                for observation in state.tool_observations
                if observation.status != ToolCallStatus.SUCCEEDED
            ],
            "coverage": {
                "completed_task_ids": sorted(completed_ids),
                "remaining_task_ids": [
                    item.task_id for item in (state.plan.tasks if state.plan else [])
                    if item.task_id not in completed_ids
                ],
                "current_completion_criteria": task.completion_criteria,
                "current_missing_information": list(state.plan.missing_information) if state.plan else [],
            },
            "latest_progress_summary": state.progress_summaries[-1] if state.progress_summaries else {},
        }

    @staticmethod
    def has_executable_replan_tasks(tasks: list[TaskSpec], unmet_gaps: list[str]) -> bool:
        """仅接受至少包含一项可运行专家任务的重规划，避免空计划伪装为补救。"""
        if not tasks or not unmet_gaps:
            return False
        return all(task.task_id and task.objective and isinstance(task.agent, AgentName) for task in tasks)

    @staticmethod
    def build_progress_summary(state: DecisionState, pool: EvidencePool,
                               latest_result) -> dict[str, Any]:
        """在每个专家任务结束后生成总控、重规划与裁判共享的公开进度摘要。"""
        all_tasks = [
            {"task_id": task.task_id, "objective": task.objective, "agent": task.agent.value,
             "completion_criteria": task.completion_criteria}
            for task in (state.plan.tasks if state.plan else [])
        ]
        completed = [
            {"task_id": result.task_id, "agent": result.agent_name.value,
             "findings": result.findings, "completion_status": result.completion_status.value}
            for result in state.agent_results
            if result.completion_status in {TaskStatus.COMPLETED, TaskStatus.COMPLETED_WITH_GAPS}
        ]
        information = [
            finding for result in state.agent_results for finding in result.findings if finding
        ]
        for evidence in pool.list():
            if evidence.status in {EvidenceStatus.CONFIRMED, EvidenceStatus.UNVERIFIED} and evidence.value:
                rendered = str(evidence.value)
                if rendered not in information:
                    information.append(rendered)
        # completed_with_gaps 可以继续下游，但其缺口仍需显式暴露给总控决定是否重规划。
        missing = list(state.plan.missing_information) if state.plan else []
        for result in state.agent_results:
            if result.completion_status != TaskStatus.COMPLETED:
                missing.extend(result.uncertainties or [f"任务 {result.task_id} 尚未完成"])
        waiting_tasks = [task for task in (state.plan.tasks if state.plan else []) if task.task_id not in {item["task_id"] for item in completed}]
        if latest_result.completion_status not in {TaskStatus.COMPLETED, TaskStatus.COMPLETED_WITH_GAPS}:
            next_step: str | list[dict[str, str]] = "根据未满足条件和失败原因生成可执行补充任务；无可执行任务时进入核验与最终判断。"
        elif waiting_tasks:
            next_step = [{"task_id": task.task_id, "objective": task.objective} for task in waiting_tasks]
        else:
            next_step = "核验重要证据并依据已获得信息形成最终判断。"
        return {
            "用户问题": state.request.query,
            "为完成这一问题计划的全部任务有": all_tasks,
            "目前已完成的任务有": completed,
            "目前得到的信息": list(dict.fromkeys(information)),
            "仍缺少或存在冲突的信息": list(dict.fromkeys(item for item in missing if item)),
            "下一步应该做": next_step,
        }

    @staticmethod
    def _controller_execution_context(state: DecisionState, pool: EvidencePool) -> dict[str, Any]:
        """给重规划和最终整合模型提供最近一次可审计进度及成功证据。"""
        return {
            "latest_progress_summary": state.progress_summaries[-1] if state.progress_summaries else {},
            "progress_summaries": state.progress_summaries,
            "successful_evidence": [
                {"claim": item.claim, "value": item.value, "source": item.source, "tool": item.tool}
                for item in pool.list() if item.status in {EvidenceStatus.CONFIRMED, EvidenceStatus.UNVERIFIED}
            ],
              "related_memory_and_hitl": {
                "memory": state.memory.model_dump(mode="json"),
                  "hitl": state.request.context.get("hitl", {}),
              },
              "information_coverage": state.information_coverage,
              "information_targets": state.information_targets,
              "evidence_ledger": [
                  observation.model_dump(mode="json") for observation in state.tool_observations
                  if DecisionGraph._observation_is_referenceable(observation)
              ],
          }

    async def _wait_for_human_input(self, state: DecisionState, request: HITLRequest,
                                    publisher: TracePublisher | None) -> HITLRequest:
        """持久化并等待有限时间的用户补充；跳过和超时均可安全恢复执行。"""
        if len(state.hitl_requests) >= self.judge.model_adapter.settings.hitl_request_limit:
            resolved = request.model_copy(update={"status": HITLStatus.SKIPPED})
            state.hitl_requests.append(resolved)
            await self._event(state, "hitl_skipped", "已跳过补充信息请求", "本次决策已达到人工补充上限，将基于现有资料继续。", {"request": resolved.model_dump(mode="json")}, publisher)
            return resolved
        state.hitl_requests.append(request)
        await self._transition(state, WorkflowStatus.WAITING_FOR_INPUT, {"request": request.model_dump(mode="json")}, publisher)
        await self._event(state, "hitl_requested", "需要你补充信息", request.rationale,
                          {"request": request.model_dump(mode="json"), "timeout_seconds": self.judge.model_adapter.settings.hitl_timeout_seconds}, publisher)
        loop = asyncio.get_running_loop()
        waiter: asyncio.Future[HITLResponse] = loop.create_future()
        key = (state.decision_id, request.request_id)
        self._hitl_waiters[key] = waiter
        try:
            response = await asyncio.wait_for(waiter, timeout=self.judge.model_adapter.settings.hitl_timeout_seconds)
            status = HITLStatus.SKIPPED if response.skip else HITLStatus.ANSWERED
            resolved = request.model_copy(update={"status": status, "response_values": response.values, "free_text": response.free_text})
        except asyncio.TimeoutError:
            resolved = request.model_copy(update={"status": HITLStatus.TIMED_OUT})
        finally:
            self._hitl_waiters.pop(key, None)
        state.hitl_requests[-1] = resolved
        await self._transition(state, WorkflowStatus.EXECUTING, {"hitl_status": resolved.status.value}, publisher)
        await self._event(state, "hitl_resolved", "补充信息阶段已结束",
                          "已收到补充信息。" if resolved.status == HITLStatus.ANSWERED else "用户跳过或等待超时，将标记不确定性后继续。",
                          {"request": resolved.model_dump(mode="json")}, publisher)
        return resolved

    async def _transition(self, state: DecisionState, target: WorkflowStatus, payload: dict[str, Any],
                          publisher: TracePublisher | None = None) -> None:
        previous = state.status if state.checkpoint_version else None
        state.status = target; state.checkpoint_version += 1
        event = WorkflowEvent(event_id=str(uuid4()), decision_id=state.decision_id,
                              from_state=previous, to_state=target,
                              kind="workflow_transition", title=self._state_title(target),
                              summary=self._state_summary(target, payload), payload=payload)
        await self._publish(event, publisher)
        self.working.save(state.decision_id, state.model_dump(mode="json"), state.checkpoint_version)

    async def _checkpoint(self, state: DecisionState, payload: dict[str, Any],
                          publisher: TracePublisher | None = None) -> None:
        state.checkpoint_version += 1
        self.working.save(state.decision_id, state.model_dump(mode="json"), state.checkpoint_version)
        await self._publish(WorkflowEvent(event_id=str(uuid4()), decision_id=state.decision_id,
            from_state=state.status, to_state=state.status, kind="checkpoint", title="已保存执行检查点",
            summary="当前任务状态已写入本地数据库，可用于后续继续执行。", payload=payload), publisher)

    async def _event(self, state: DecisionState, kind: str, title: str, summary: str,
                     payload: dict[str, Any], publisher: TracePublisher | None = None) -> None:
        """创建一条不改变工作流状态的可展示事件。"""
        await self._publish(WorkflowEvent(
            event_id=str(uuid4()), decision_id=state.decision_id, from_state=state.status,
            to_state=state.status, kind=kind, title=title, summary=summary, payload=payload,
        ), publisher)

    async def _publish(self, event: WorkflowEvent, publisher: TracePublisher | None) -> None:
        """保证流式模式先持久化、后通知浏览器；普通模式仅持久化。"""
        if publisher is None:
            self.traces.save(event)
            return
        await publisher.publish(event)

    @staticmethod
    def _profile_source_texts(request: DecisionRequest) -> list[str]:
        """取出当前决策中所有用户亲自输入的、可追溯的画像候选文本。"""
        values = request.context.get("profile_source_texts", [])
        return [value for value in values if isinstance(value, str) and value.strip()] if isinstance(values, list) else []

    @staticmethod
    def _temporal_context(request: DecisionRequest) -> dict[str, Any]:
        """读取已经写入请求的时间上下文；异常外部输入不会影响画像抽取。"""
        context = request.context.get("temporal_context")
        return context if isinstance(context, dict) else {}

    @staticmethod
    def _hitl_texts(values: dict[str, Any]) -> list[str]:
        """从 HITL 的结构化字段和自由文本取出用户实际填写的字符串。"""
        return [value for value in values.values() if isinstance(value, str) and value.strip()]

    @classmethod
    def _with_user_texts(cls, request: DecisionRequest, texts: list[str],
                         *, extra_context: dict[str, Any] | None = None) -> DecisionRequest:
        """累积用户文本和相对日期上下文，确保后续每个模型阶段看到相同事实。"""
        context = {**request.context, **(extra_context or {})}
        sources = cls._profile_source_texts(request)
        temporal = cls._temporal_context(request)
        for text in texts:
            if not isinstance(text, str) or not text.strip():
                continue
            clean = text.strip()
            if clean not in sources:
                sources.append(clean)
            temporal = merge_temporal_context(temporal, clean)
        context["profile_source_texts"] = sources
        context["temporal_context"] = temporal
        return request.model_copy(update={"context": context})

    @staticmethod
    def _state_title(status: WorkflowStatus) -> str:
        """将工作流枚举映射为聊天页面可直接显示的中文阶段名。"""
        return {
            WorkflowStatus.RECEIVED: "已接收决策问题", WorkflowStatus.CLASSIFIED: "已识别决策类型",
            WorkflowStatus.MEMORY_RETRIEVED: "已读取相关记忆", WorkflowStatus.SKILL_LOADED: "已载入决策 Skill",
            WorkflowStatus.PLANNED: "已完成 Plan-and-Execute 规划", WorkflowStatus.EXECUTING: "开始执行专家任务", WorkflowStatus.WAITING_FOR_INPUT: "等待你补充信息",
            WorkflowStatus.REPLANNING: "正在根据执行结果调整计划", WorkflowStatus.VERIFYING: "正在核验证据",
            WorkflowStatus.DEBATING: "正在进行风险与观点审查", WorkflowStatus.JUDGING: "正在汇总形成判断",
            WorkflowStatus.COMPLETED: "已完成决策分析", WorkflowStatus.ARCHIVED: "已归档本次决策",
            WorkflowStatus.FAILED: "决策执行失败",
        }[status]

    @staticmethod
    def _state_summary(status: WorkflowStatus, payload: dict[str, Any]) -> str:
        """为状态变更生成简洁、公开且不含隐藏思维链的说明。"""
        summaries = {
            WorkflowStatus.RECEIVED: "问题已进入工作流。", WorkflowStatus.CLASSIFIED: "总控已选择本次决策的领域与分析策略。",
            WorkflowStatus.MEMORY_RETRIEVED: "已检索与当前问题相关的长期偏好和历史经验。",
            WorkflowStatus.SKILL_LOADED: "已选择该领域的标准分析步骤和完成条件。",
            WorkflowStatus.PLANNED: "已将目标拆解为有依赖关系的专家任务。", WorkflowStatus.EXECUTING: "将依次执行满足依赖条件的任务。", WorkflowStatus.WAITING_FOR_INPUT: "补充信息将用于恢复当前计划或专家任务。",
            WorkflowStatus.REPLANNING: "外部信息不足或不可用，已调整后续判断依据。",
            WorkflowStatus.VERIFYING: "正在检查证据的冲突、时效性和可验证性。",
            WorkflowStatus.DEBATING: "正在从反方角度审查关键风险与约束。",
            WorkflowStatus.JUDGING: "正在将已确认事实、推断、偏好和风险汇总为结论。",
            WorkflowStatus.COMPLETED: "已得到决策报告。", WorkflowStatus.ARCHIVED: "报告、轨迹和记忆已保存到本地。",
            WorkflowStatus.FAILED: "流程未能完成；已保留可用的轨迹信息。",
        }
        return summaries[status]

    def _response(self, state: DecisionState) -> DecisionResponse:
        return DecisionResponse(
            decision_id=state.decision_id, decision_type=state.decision_type,
            status=state.status, report=state.report, plan=state.plan,
            events=self.traces.list(state.decision_id), activated_agents=state.activated_agents, candidates=state.request.candidates,
        )
