"""供可调用工具的专家 Agent 共享的、以任务为边界的 ReAct 执行框架。"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from app.trace_stream import TraceSink, emit_to_sink
from models.contracts import AgentName, AgentResult, HITLField, HITLRequest, InformationCoverageUpdate, MemoryContext, ObservationRelevance, ReActDecision, TaskSpec, TaskStatus, ToolBindingAssessment, ToolCallStatus, ToolObservation


class ToolAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool_name: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)


class AgentContext(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)
    decision_id: str
    gateway: Any = None
    memory: MemoryContext = Field(default_factory=MemoryContext)
    evidence_pool: Any = None
    observations: list[ToolObservation] = Field(default_factory=list)
    request_context: dict[str, Any] = Field(default_factory=dict)
    trace_sink: TraceSink | None = None
    model_adapter: Any = None
    request: Any = None
    available_tools: list[Any] = Field(default_factory=list)
    execution_context: dict[str, Any] = Field(default_factory=dict)
    human_input_handler: Any = None
    replan_reason: str | None = None
    information_coverage: dict[str, dict[str, Any]] = Field(default_factory=dict)
    information_targets: list[dict[str, Any]] = Field(default_factory=list)
    # 以工具观察 ID 索引的可引用资料账本，供纯归纳目标跨目标结算时核验来源。
    evidence_ledger: dict[str, dict[str, Any]] = Field(default_factory=dict)

    async def trace(self, kind: str, title: str, summary: str, payload: dict[str, Any]) -> None:
        """向所属决策的实时轨迹写入一条可公开展示的行动说明。"""
        await emit_to_sink(self.trace_sink, kind, title, summary, payload)

    def apply_coverage_updates(self, updates: list[InformationCoverageUpdate]) -> list[dict[str, Any]]:
        """用最新观察替代旧的部分资料，同时把被替代版本保留为可审计历史。"""
        changes: list[dict[str, Any]] = []
        for update in updates:
            previous = self.information_coverage.get(update.target_key)
            if previous and previous.get("status") == "complete" and update.status == "partial":
                continue
            history = list(previous.get("history", [])) if previous else []
            if previous and previous.get("latest_summary"):
                history.append({
                    "status": "superseded",
                    "previous_status": previous.get("status"),
                    "summary": previous["latest_summary"],
                })
            record = {
                "target": update.target,
                "status": update.status,
                "latest_summary": update.summary,
                "history": history,
            }
            self.information_coverage[update.target_key] = record
            changes.append({"target_key": update.target_key, **record})
        self.execution_context["information_coverage"] = self.information_coverage
        return changes


class BaseReActAgent:
    name = AgentName.EVIDENCE_RESEARCH
    max_tool_calls = 3
    max_binding_calls = 3

    async def execute(self, task: TaskSpec, context: AgentContext) -> AgentResult:
        """依次完成专家内部信息目标，每项目标独立拥有三次 MCP 调用额度。"""
        if task.agent != self.name:
            raise ValueError(f"task {task.task_id} is assigned to {task.agent.value}, not {self.name.value}")
        await self._ensure_information_targets(task, context)
        used = 0
        attempted_actions: dict[tuple[str, str, str], ToolCallStatus] = {}
        for target in context.information_targets:
            if context.replan_reason or target.get("status") == "complete":
                continue
            target.setdefault("status", "pending")
            target.setdefault("tool_calls_used", 0)
            target.setdefault("binding_calls_used", 0)
            target.setdefault("observation_start_index", len(context.observations))
            target_id = str(target["target_id"])
            context.execution_context["active_information_target"] = target
            rounds = 0
            while (
                context.gateway is not None
                and target["tool_calls_used"] < self.max_tool_calls
                and target["binding_calls_used"] < self.max_binding_calls
                and rounds < (self.max_tool_calls + self.max_binding_calls) * 2
            ):
                rounds += 1
                decision = await self._decide(task, context, self.max_tool_calls - target["tool_calls_used"])
                has_usable_observation = any(
                    self._is_semantically_usable(item) and item.result_summary
                    for item in context.observations[target["observation_start_index"]:]
                )
                # 状态账本优先于模型下一动作：已完成目标绝不能继续消耗自己的额度或承接另一目标的查询。
                if target.get("status") == "complete":
                    break
                # 当前目标只能由 Settlement 的 target_complete 结束，ReAct 不能自行以 partial/blocked 结算。
                if decision.action == "finish":
                    if target.get("status") == "complete":
                        break
                    resolution_error = "当前信息目标只能由 Settlement 在所有完成条件满足时结束；请继续补齐当前缺口、请求重规划或请求用户补充。"
                    context.execution_context["react_validation_error"] = resolution_error
                    context.observations.append(ToolObservation(
                        call_id=str(uuid4()), decision_id=context.decision_id, task_id=task.task_id,
                        target_id=target_id, agent=self.name, tool_name="react_target_resolution",
                        status=ToolCallStatus.FAILED, error=resolution_error,
                    ))
                    await context.trace(
                        "react_resolution_repair", "专家目标结算正在修正", resolution_error,
                        {"task_id": task.task_id, "target_id": target_id, "validation_error": resolution_error},
                    )
                    continue
                if decision.action == "request_replan":
                    context.replan_reason = decision.public_summary
                    await context.trace("react_replan", "专家请求重新规划", decision.public_summary, {"task_id": task.task_id, "agent": self.name.value})
                    break
                if decision.action == "request_human_input":
                    if context.human_input_handler is None:
                        context.replan_reason = "缺少关键用户信息，且当前执行环境无法等待用户补充"; break
                    request = HITLRequest(request_id=str(uuid4()), decision_id=context.decision_id, source_agent=self.name, stage="execution", question=decision.hitl_question or "请补充决策信息", rationale=decision.hitl_rationale or decision.public_summary, fields=decision.hitl_fields)
                    await context.human_input_handler(request)
                    continue
                action = ToolAction(tool_name=decision.tool_name or "", arguments=decision.arguments)
                action_key = (target_id, action.tool_name, json.dumps(action.arguments, ensure_ascii=False, sort_keys=True, default=str))
                # 已成功的同参调用允许模型在新上下文下复用或补充；只有已失败的重复才拦截。
                if action_key in attempted_actions and attempted_actions[action_key] != ToolCallStatus.SUCCEEDED:
                    context.observations.append(ToolObservation(call_id=str(uuid4()), decision_id=context.decision_id, task_id=task.task_id, agent=self.name, tool_name=action.tool_name, arguments=action.arguments, status=ToolCallStatus.FAILED, error="重复工具调用已阻止：请改用不同的工具或参数、请求重新规划，或结束当前目标。"))
                    continue
                binding = await self._assess_tool_binding(task, context, target, action)
                target["binding_calls_used"] += 1
                if not binding.bound:
                    reason = binding.reason or "工具参数未能绑定到当前信息目标。"
                    context.execution_context["react_validation_error"] = reason
                    observation = ToolObservation(
                        call_id=str(uuid4()), decision_id=context.decision_id, task_id=task.task_id,
                        target_id=target_id, agent=self.name, tool_name=action.tool_name,
                        arguments=action.arguments, status=ToolCallStatus.FAILED, error=reason,
                    )
                    context.observations.append(observation)
                    attempted_actions[action_key] = observation.status
                    await context.trace(
                        "tool_binding_rejected", "工具参数未绑定当前信息目标", reason,
                        {"task_id": task.task_id, "target_id": target_id, "tool": action.tool_name,
                         "arguments": action.arguments, "binding_calls_used": target["binding_calls_used"],
                         "binding_call_limit": self.max_binding_calls},
                    )
                    continue
                await context.trace(
                    "tool_binding_accepted", "工具参数已绑定当前信息目标", "调用前语义预检通过，准备调用 MCP。",
                    {"task_id": task.task_id, "target_id": target_id, "tool": action.tool_name,
                     "binding_calls_used": target["binding_calls_used"], "binding_call_limit": self.max_binding_calls},
                )
                await context.trace("react_action", "ReAct 选择下一步行动", "正在为当前信息目标补齐资料。", {"task_id": task.task_id, "target_id": target_id, "agent": self.name.value, "tool": action.tool_name, "arguments": action.arguments})
                observation = await context.gateway.call_tool(self.name, action.tool_name, action.arguments, decision_id=context.decision_id, task_id=task.task_id, target_id=target_id, trace_sink=context.trace_sink)
                observation = observation.model_copy(update={"target_id": target_id})
                observation = await self._summarize_observation(task, context, observation)
                observation = await self._assess_observation(task, context, target, observation)
                context.observations.append(observation)
                self._record_usable_observation(context, observation)
                attempted_actions[action_key] = observation.status
                used += 1; target["tool_calls_used"] += 1
                await context.trace("tool_observation", "工具观察结果", observation.result_summary or observation.error or "工具未返回可用内容。", {"task_id": task.task_id, "target_id": target_id, "tool": observation.tool_name, "arguments": observation.arguments, "status": observation.status.value, "semantic_status": observation.semantic_status.value if observation.semantic_status else None, "semantic_summary": observation.semantic_summary, "result_summary": observation.result_summary, "error": observation.error})
                # 结算与下一轮 ReAct 解耦：每条语义可用工具观察刚落账就立即由专用模型更新当前子目标状态。
                if self._should_settle_observation(observation) and observation.result_summary:
                    target_was_settled = await self._settle_current_target_after_observation(
                        task, context, target, observation,
                    )
                    if target_was_settled or target.get("status") == "complete":
                        break
            if target.get("status") not in {"complete", "partial", "blocked"}:
                exhausted = target["tool_calls_used"] >= self.max_tool_calls or target["binding_calls_used"] >= self.max_binding_calls
                target.update({
                    "status": "blocked",
                    "latest_summary": target.get("latest_summary") or (
                        "该信息目标的 MCP 或预检调用额度已耗尽。" if exhausted else "专家未在现有资料下完成该信息目标。"
                    ),
                })
                await context.trace("information_target_status", "信息目标已阻塞", "该目标未达到完成条件，已保留现有资料与缺口。", {"task_id": task.task_id, "target_id": target_id, "status": "blocked", "tool_calls_used": target["tool_calls_used"], "latest_summary": target["latest_summary"]})
        return await self.finish(task, context, used)

    async def _assess_tool_binding(self, task: TaskSpec, context: AgentContext,
                                   target: dict[str, Any], action: ToolAction) -> ToolBindingAssessment:
        """在任何 MCP 调用前，以当前目标为唯一语义范围验证工具参数。"""
        assessor = getattr(context.model_adapter, "assess_tool_binding_or_fallback", None)
        if not callable(assessor) or context.request is None:
            # 兼容不含模型的本地单元测试；实际 ModelAdapter 在离线时会保守拒绝。
            return ToolBindingAssessment(bound=True)
        selected_tool: dict[str, Any] = {"name": action.tool_name}
        for item in context.available_tools:
            raw = item.model_dump(mode="json") if hasattr(item, "model_dump") else item
            if isinstance(raw, dict) and raw.get("name") == action.tool_name:
                selected_tool = {
                    "name": raw.get("name"), "description": raw.get("description", ""),
                    "input_schema": raw.get("input_schema", {}),
                }
                break
        await context.trace(
            "tool_binding_requested", "正在预检工具参数", "调用 MCP 前正在确认参数是否服务当前信息目标。",
            {"task_id": task.task_id, "target_id": target.get("target_id"), "tool": action.tool_name,
             "arguments": action.arguments},
        )
        return await assessor(
            request=context.request, task=task, target={
                key: target[key] for key in ("target_id", "objective", "completion_criteria", "status", "latest_summary")
                if key in target
            },
            tool=selected_tool, arguments=action.arguments,
        )

    async def _settle_current_target_after_observation(self, task: TaskSpec, context: AgentContext,
                                                        target: dict[str, Any], observation: ToolObservation) -> bool:
        """仅用当前目标已支持的观察逐条件结算；partial 必须继续 ReAct。"""
        submit = getattr(context.model_adapter, "settle_current_target_after_observation_or_none", None)
        target_id = str(target["target_id"])
        if not callable(submit):
            return False
        current_target = {
            key: target[key] for key in ("target_id", "objective", "completion_criteria", "status", "latest_summary") if key in target
        }
        target_observations = [
            item.model_dump(mode="json") for item in context.observations
            if item.target_id == target_id and item.result_summary and self._is_semantically_usable(item)
        ]
        await context.trace(
            "target_settlement_requested", "正在结算信息目标",
            "工具已返回语义可用资料；正在由专用结算节点更新当前信息目标状态。",
            {"task_id": task.task_id, "target_id": target_id},
        )
        submission = await submit(
            current_target=current_target,
            target_observations=target_observations,
            existing_coverage=context.information_coverage.get(target_id, {}),
        )
        if submission is None:
            await context.trace(
                "target_settlement_failed", "信息目标结算失败",
                "专用结算节点没有返回有效 JSON；将保留当前资料并继续既有执行。",
                {"task_id": task.task_id, "target_id": target_id},
            )
            return False
        expected_criteria = list(target.get("completion_criteria", task.completion_criteria))
        returned_criteria = [item.criterion for item in submission.criteria]
        if expected_criteria and set(returned_criteria) != set(expected_criteria):
            await context.trace(
                "target_settlement_failed", "信息目标结算无效",
                "结算内容没有逐项覆盖当前信息目标的完成条件；将保留当前资料并继续既有执行。",
                {"task_id": task.task_id, "target_id": target_id},
            )
            return False
        status = "complete" if submission.target_complete else "partial"
        target.update({"status": status, "latest_summary": submission.summary})
        changes = context.apply_coverage_updates([InformationCoverageUpdate(
            target_key=target_id, target=str(target.get("objective", target_id)),
            status=status, summary=submission.summary,
        )])
        await context.trace(
            "information_coverage_updated", "已更新当前信息目标覆盖状态",
            "专用结算节点已逐项评估当前目标完成条件。",
            {"task_id": task.task_id, "target_id": target_id, "updates": changes,
             "criteria": [item.model_dump(mode="json") for item in submission.criteria],
             "missing_information": submission.missing_information,
             "target_complete": submission.target_complete},
        )
        await context.trace(
            "target_settlement_applied", "已结算信息目标",
            "目标仅在所有完成条件满足时提前结束；partial 会回到 ReAct 补齐资料。",
            {"task_id": task.task_id, "target_id": target_id,
             "coverage_status": submission.coverage_status, "target_complete": submission.target_complete},
        )
        return submission.target_complete

    async def _ensure_information_targets(self, task: TaskSpec, context: AgentContext) -> None:
        """仅在没有已持久化目标时让模型生成专家内部计划，并立即发出前端可见轨迹。"""
        if context.information_targets:
            return
        if context.model_adapter is not None and context.request is not None:
            plan = await context.model_adapter.information_plan_or_fallback(task=task, request=context.request, memory=context.memory, tools=context.available_tools, execution_context=context.execution_context)
            context.information_targets = [item.model_dump(mode="json") for item in plan.targets]
        else:
            context.information_targets = [{"target_id": f"{task.task_id}-primary", "objective": task.objective, "completion_criteria": task.completion_criteria, "status": "pending", "tool_calls_used": 0}]
        await context.trace("expert_information_plan", "专家信息目标计划", "专家已拆分当前任务需要补齐的信息目标。", {"task_id": task.task_id, "agent": self.name.value, "targets": context.information_targets})

    async def _summarize_observation(self, task: TaskSpec, context: AgentContext,
                                     observation: ToolObservation) -> ToolObservation:
        """将任意长工具结果或错误替换为模型提炼的公开摘要，避免 Trace 仅显示截断开头。"""
        raw_result = observation.result_summary or observation.error
        if (
            not raw_result
            or len(raw_result) <= 1500
            or context.model_adapter is None
            or context.request is None
        ):
            return observation
        summary = await context.model_adapter.summarize_tool_result(
            query=context.request.query, task_objective=task.objective,
            tool_name=observation.tool_name, raw_result=raw_result,
        )
        field = "result_summary" if observation.result_summary else "error"
        return observation.model_copy(update={field: summary})

    async def _assess_observation(self, task: TaskSpec, context: AgentContext,
                                  target: dict[str, Any], observation: ToolObservation) -> ToolObservation:
        """对传输成功的结果调用 LLM 语义核验，避免无关内容污染当前证据。"""
        if observation.status != ToolCallStatus.SUCCEEDED or not observation.result_summary:
            return observation
        if context.model_adapter is None or context.request is None:
            return observation.model_copy(update={
                "semantic_status": ObservationRelevance.UNVERIFIABLE,
                "semantic_summary": "模型不可用，无法核验本次工具返回是否支持当前信息目标。",
                "semantic_missing_information": list(target.get("completion_criteria", task.completion_criteria)),
            })
        assessment = await context.model_adapter.assess_observation_or_fallback(
            request=context.request, task=task, target=target, observation=observation,
        )
        return observation.model_copy(update={
            "semantic_status": assessment.relevance,
            "semantic_summary": assessment.summary,
            "semantic_missing_information": assessment.missing_information,
            "supports_current_target": assessment.supports_current_target,
            "coverage_contribution": assessment.coverage_contribution,
            "related_target_ids": assessment.related_target_ids,
        })

    async def _apply_target_resolution(self, task: TaskSpec, context: AgentContext,
                                       target: dict[str, Any], decision: ReActDecision,
                                       has_usable_observation: bool) -> str | None:
        """验证并持久化专家对当前目标的显式结束结算；错误会回传下一轮 ReAct 修正。"""
        resolution = decision.target_resolution
        target_id = str(target["target_id"])
        if resolution is None:
            return "finish 必须提供 target_resolution，说明当前信息目标是 complete、partial 或 blocked。"
        if resolution.target_id != target_id:
            return f"target_resolution.target_id 必须是当前信息目标 {target_id}，不能结算其他目标。"
        # 比较或归纳目标可能无需本地新工具调用，可显式引用同一决策中已通过语义核验的资料。
        referenced_observations, invalid_refs = self._resolve_evidence_refs(context, resolution.evidence_refs)
        if invalid_refs:
            return f"target_resolution.evidence_refs 包含不存在、非本决策或未通过语义核验的观察：{', '.join(invalid_refs)}。"
        if resolution.status == "complete" and not (has_usable_observation or referenced_observations):
            return (
                "当前目标没有经语义核验为 relevant 或 partial 的直接观察，也没有有效的 "
                "target_resolution.evidence_refs；不能标记为 complete，请改为 partial 或 blocked。"
            )
        target.update({"status": resolution.status, "latest_summary": resolution.summary})
        if resolution.status in {"complete", "partial"}:
            changes = context.apply_coverage_updates([InformationCoverageUpdate(
                target_key=target_id, target=str(target.get("objective", target_id)),
                status=resolution.status, summary=resolution.summary,
            )])
            await context.trace(
                "information_target_resolved", "专家已结算信息目标", resolution.summary,
                {"task_id": task.task_id, "target_id": target_id, "resolution": resolution.model_dump(mode="json"), "updates": changes},
            )
        else:
            await context.trace(
                "information_target_status", "信息目标已阻塞", resolution.summary,
                {"task_id": task.task_id, "target_id": target_id, "status": "blocked", "missing_information": resolution.missing_information},
            )
        return None

    @staticmethod
    def _record_usable_observation(context: AgentContext, observation: ToolObservation) -> None:
        """把通过语义核验的观察登记为可被后续归纳目标引用的公开资料。"""
        if not BaseReActAgent._is_referenceable_observation(observation) or not observation.result_summary:
            return
        context.evidence_ledger[observation.call_id] = observation.model_dump(mode="json")
        context.execution_context["evidence_ledger"] = list(context.evidence_ledger.values())

    @staticmethod
    def _resolve_evidence_refs(context: AgentContext, evidence_refs: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
        """解析结算引用并拒绝不存在、跨决策或语义不可用的工具观察。"""
        if not evidence_refs:
            return [], []
        available: dict[str, dict[str, Any]] = {}
        for observation in context.observations:
            available[observation.call_id] = observation.model_dump(mode="json")
        available.update(context.evidence_ledger)
        inherited = context.execution_context.get("evidence_ledger", [])
        inherited_items = inherited.values() if isinstance(inherited, dict) else inherited
        for item in inherited_items:
            if isinstance(item, dict) and isinstance(item.get("call_id"), str):
                available.setdefault(item["call_id"], item)
        resolved: list[dict[str, Any]] = []
        invalid: list[str] = []
        for reference in evidence_refs:
            observation = available.get(reference)
            if (
                observation is None
                or observation.get("decision_id") != context.decision_id
                or not BaseReActAgent._observation_dict_is_referenceable(observation)
                or not observation.get("result_summary")
            ):
                invalid.append(reference)
            else:
                resolved.append(observation)
        return resolved, invalid

    async def _decide(self, task: TaskSpec, context: AgentContext, remaining_calls: int) -> ReActDecision:
        """模型可用时让专家自主选择下一步；本地规则仅作为离线回退。"""
        if context.model_adapter is not None and context.request is not None:
            active_target = context.execution_context.get("active_information_target", {})
            active_target_id = active_target.get("target_id") if isinstance(active_target, dict) else None
            previous_task_history = [
                item for item in context.execution_context.get("current_task_history", [])
                if not isinstance(item, dict) or item.get("target_id") in {None, active_target_id}
            ]
            current_task_observations = [
                item.model_dump(mode="json") for item in context.observations
                if item.task_id == task.task_id and item.target_id == active_target_id
            ]
            task_observations = [*previous_task_history, *current_task_observations]
            coverage = dict(context.execution_context.get("coverage", {}))
            coverage["current_completion_criteria"] = list(active_target.get("completion_criteria", task.completion_criteria))
            coverage["current_successful_results"] = [
                item["result_summary"] for item in task_observations
                if self._observation_dict_is_usable(item) and item.get("result_summary")
            ]
            coverage["current_failed_results"] = [
                item.get("error") for item in task_observations
                if item.get("status") != ToolCallStatus.SUCCEEDED.value and item.get("error")
            ]
            execution_context = {
                **context.execution_context, "current_task_history": self._prompt_task_history(task_observations),
                "coverage": coverage,
            }
            if context.execution_context.get("react_validation_error"):
                execution_context["react_validation_error"] = context.execution_context["react_validation_error"]
            execution_context["react_context"] = self._react_context_view(
                task, context, task_observations, coverage,
            )
            return await context.model_adapter.react_or_fallback(
                task=task, request=context.request, memory=context.memory,
                observations=task_observations,
                tools=context.available_tools, remaining_calls=remaining_calls,
                remaining_binding_calls=max(0, self.max_binding_calls - int(active_target.get("binding_calls_used", 0))),
                execution_context=execution_context,
            )
        action = await self.next_action(task, context)
        return ReActDecision(
            action="call_tool" if action is not None else "finish",
            public_summary="本地降级逻辑继续完成当前任务。" if action is not None else "当前任务无需外部工具。",
            tool_name=action.tool_name if action else None, arguments=action.arguments if action else {},
            target_resolution=None if action else {
                "target_id": str(context.execution_context.get("active_information_target", {}).get("target_id") or f"{task.task_id}-primary"),
                "status": "blocked", "summary": "本地降级逻辑没有可验证的外部资料。",
                "missing_information": list(task.completion_criteria),
            },
        )

    async def next_action(self, task: TaskSpec, context: AgentContext) -> ToolAction | None:
        return None

    def needs_more(self, task: TaskSpec, context: AgentContext) -> bool:
        return False

    @staticmethod
    def _prompt_task_history(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """将历史观察转换成模型可读记录，避免反复注入过期失败详情。"""
        history: list[dict[str, Any]] = []
        for item in observations:
            record = {
                "tool_name": item.get("tool_name"),
                "arguments": item.get("arguments", {}),
                "status": item.get("status"),
            }
            if item.get("result_summary"):
                record["result_summary"] = item["result_summary"]
            if item.get("semantic_status"):
                record["semantic_status"] = item["semantic_status"]
            if item.get("semantic_summary"):
                record["semantic_summary"] = item["semantic_summary"]
            history.append(record)
        return history

    async def finish(self, task: TaskSpec, context: AgentContext, used: int) -> AgentResult:
        """按可用资料和剩余缺口结算专家任务，避免局部不足抹掉已取得结论。"""
        successes = [item for item in context.observations if item.task_id == task.task_id and self._is_semantically_usable(item)]
        failures = [item for item in context.observations if item.task_id == task.task_id and item.status != ToolCallStatus.SUCCEEDED]
        uncertainties = [item.error or f"{item.tool_name} 不可用" for item in failures]
        uncertainties.extend(
            item.semantic_summary or f"{item.tool_name} 返回内容未通过当前目标的语义核验"
            for item in context.observations
            if item.task_id == task.task_id and item.status == ToolCallStatus.SUCCEEDED
            and item.semantic_status in {ObservationRelevance.IRRELEVANT, ObservationRelevance.UNVERIFIABLE}
        )
        if context.replan_reason:
            uncertainties.append(context.replan_reason)
        incomplete_targets = [item for item in context.information_targets if item.get("status") != "complete"]
        if incomplete_targets:
            uncertainties.extend(f"信息目标未完整覆盖：{item.get('objective', item.get('target_id'))}" for item in incomplete_targets)
            uncertainties.extend(
                f"信息目标 MCP 调用额度耗尽（上限 {self.max_tool_calls} 次）：{item.get('objective', item.get('target_id'))}"
                for item in incomplete_targets if item.get("tool_calls_used", 0) >= self.max_tool_calls
            )
        # 当前目标可用观察与已结算目标摘要共同构成下游可读的任务产出。
        findings = [item.result_summary for item in successes if item.result_summary]
        for target in context.information_targets:
            summary = target.get("latest_summary")
            if target.get("status") in {"complete", "partial"} and isinstance(summary, str) and summary and summary not in findings:
                findings.append(summary)
        has_usable_material = bool(findings)
        has_gaps = bool(incomplete_targets or context.replan_reason)
        # 有资料但有普通缺口不应抹掉下游综合所需的发现，因此单列 completed_with_gaps。
        completion_status = (
            TaskStatus.COMPLETED_WITH_GAPS if has_usable_material and has_gaps
            else TaskStatus.COMPLETED if has_usable_material or not task.required_capabilities
            else TaskStatus.BLOCKED
        )
        return AgentResult(
            result_id=str(uuid4()), decision_id=context.decision_id, task_id=task.task_id,
            agent_name=self.name,
            summary=(
                f"{self.name.value} 已取得可用资料但仍有公开缺口；保留成功观察 {len(successes)} 条供后续保守判断"
                if completion_status == TaskStatus.COMPLETED_WITH_GAPS
                else f"{self.name.value} 完成任务；成功观察 {len(successes)} 条"
                if completion_status == TaskStatus.COMPLETED
                else f"{self.name.value} 没有取得可用资料；保留缺口供总控判断"
            ),
            findings=findings,
            uncertainties=uncertainties,
            completion_status=completion_status,
            tool_calls_used=used,
        )

    @staticmethod
    def _react_context_view(task: TaskSpec, context: AgentContext,
                            task_observations: list[dict[str, Any]], coverage: dict[str, Any]) -> dict[str, Any]:
        """构造严格限于当前信息目标的 ReAct 公开上下文。"""
        prompt_history = BaseReActAgent._prompt_task_history(task_observations)
        active_target = dict(context.execution_context.get("active_information_target", {}))
        target_id = active_target.get("target_id")
        target_coverage = context.information_coverage.get(target_id, {}) if target_id else {}
        missing = list(active_target.get("missing_information", []))
        if not missing:
            missing = list(coverage.get("current_missing_information", []))
        if not missing:
            missing = list(active_target.get("completion_criteria", task.completion_criteria))
        hitl = context.request.context.get("hitl", {}) if context.request is not None else {}
        return {
            "用户问题": context.request.query if context.request is not None else "",
            "Task": {"task_id": task.task_id, "objective": task.objective},
            "当前 target": active_target,
            "当前 target completion_criteria": list(active_target.get("completion_criteria", task.completion_criteria)),
            "当前 target 已有 observations": prompt_history,
            "当前 target latest_summary": active_target.get("latest_summary"),
            "当前 target coverage": target_coverage,
            "当前 target missing_information": missing,
            "当前 target 剩余 MCP 调用次数": max(0, BaseReActAgent.max_tool_calls - int(active_target.get("tool_calls_used", 0))),
            "当前 target 剩余预检次数": max(0, BaseReActAgent.max_binding_calls - int(active_target.get("binding_calls_used", 0))),
            "相关记忆与 HITL 补充": {
                "memory": context.memory.model_dump(mode="json"),
                "hitl": hitl,
            },
        }

    @staticmethod
    def _is_semantically_usable(observation: ToolObservation) -> bool:
        """兼容旧记录，同时只把语义相关或部分相关的新观察视为可用资料。"""
        return observation.supports_current_target and BaseReActAgent._is_referenceable_observation(observation)

    @staticmethod
    def _should_settle_observation(observation: ToolObservation) -> bool:
        """按结算节点合同识别触发观察：仅要求工具成功且语义核验为相关或部分相关。"""
        return observation.supports_current_target and observation.status == ToolCallStatus.SUCCEEDED and observation.semantic_status in {
            ObservationRelevance.RELEVANT, ObservationRelevance.PARTIAL,
        }

    @staticmethod
    def _is_referenceable_observation(observation: ToolObservation) -> bool:
        """允许保留服务于同一决策其他目标的部分资料，但不把它误记为当前目标完成。"""
        return observation.status == ToolCallStatus.SUCCEEDED and observation.semantic_status in {
            None, ObservationRelevance.RELEVANT, ObservationRelevance.PARTIAL,
        }

    @staticmethod
    def _observation_dict_is_usable(observation: dict[str, Any]) -> bool:
        """供 Prompt 上下文重建使用的可用观察判断，与实体对象逻辑保持一致。"""
        return observation.get("supports_current_target", True) and BaseReActAgent._observation_dict_is_referenceable(observation)

    @staticmethod
    def _observation_dict_is_referenceable(observation: dict[str, Any]) -> bool:
        """在解析 evidence_refs 时接受经核验、可供其他目标使用的观察。"""
        return observation.get("status") == ToolCallStatus.SUCCEEDED.value and observation.get("semantic_status") in {
            None, ObservationRelevance.RELEVANT.value, ObservationRelevance.PARTIAL.value,
        }
