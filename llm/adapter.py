"""结构化模型调用与确定性离线推理实现。"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, TypeVar

from pydantic import BaseModel, ValidationError

from app.config import Settings
from app.temporal_context import birth_year_range_from_age
from models.contracts import (
    AgentName, AnalysisMode, AutonomousPlan, DecisionReport, DecisionRequest,
    DecisionType, Evidence, EvidenceRelationship, ExecutionPlan, ExpertInformationPlan, GeneralTaskResolution, InformationTarget, MemoryContext, ObservationAssessment, ObservationRelevance, ProfileSignalExtraction, ReActDecision, ReplanDecision, TargetResolution, TargetSettlementSubmission, TaskSpec, ToolBindingAssessment, ToolObservation, TraceSummary,
)


T = TypeVar("T", bound=BaseModel)

USER_INFORMATION_BOUNDARY = (
    "不要编造用户信息、偏好、住址、职业、专业、年龄、生日、兴趣爱好或个人背景。"
    "只有载荷中的用户原文、已提供的记忆或已标记 Evidence 明确支持时才能提及；"
    "不确定时必须明确说明不确定，不能把推测写成用户事实。"
)

# 这条通用原则用于规划、专家执行和最终整合，避免模型为了一个边缘细节耗尽有限的工具额度。
EVIDENCE_SUFFICIENCY_GUIDANCE = (
    "以可解释、可行动的建议为目标：信息达到可支持结论的参考程度即可，不必追求每项细节精确齐全。"
    "允许结合已验证信息作合理推断（保守推断），无需精确无误，但必须明确标注推断，且不得把推断写成已观察或已核验的事实；"
    "只有某个缺口会实质改变推荐、硬约束判断或风险提示时，才继续检索、追问或重规划。"
)

STRUCTURED_OUTPUT_RULES = (
    "你必须只输出一个 JSON 对象：不要 Markdown、代码围栏、解释或额外文字。"
    "对象只能使用 JSON Schema 允许的字段，必须满足所有 required、枚举、长度、范围、依赖与条件限制。"
)

AUTONOMOUS_PLAN_FIELD_CONTRACT = """
AutonomousPlan 字段合同：
- decision_type：必须是允许的决策类型之一。
- skill_name：只能是技能目录中出现的名称，或 null。
- planning_summary：面向用户的简短公开规划依据；不得包含私有思维链。
- plan.goal：本次决策目标。
- plan.tasks：任务数组；每项必须有唯一 task_id、objective、agent；dependencies 只能引用本数组中已有 task_id。
- task.agent：只能选择允许的专家 Agent。
- task.required_capabilities：只能选择该 Agent 实际允许且当前已发现的能力。
- plan.missing_information：确实缺少、会显著影响结论的信息。
- plan.requires_verification / requires_debate：布尔值。
- plan.replan_conditions：在何种可观察条件下应重新规划。
- hitl_question / hitl_rationale / hitl_fields：只有缺少用户补充会显著提高结论质量时才填写；字段最多三个，否则全部为 null/空数组。
""".strip()


class DeterministicReasoner:
    """只对用户输入、历史记忆或已引用证据评分，绝不编造实时外部事实。"""

    def plan(self, request: DecisionRequest, decision_type: DecisionType = DecisionType.GENERAL) -> ExecutionPlan:
        return ExecutionPlan(goal=request.query, tasks=[
            TaskSpec(task_id="preference", objective="整理显式偏好与历史偏好", agent=AgentName.PREFERENCE, completion_criteria=["偏好已结构化"]),
            TaskSpec(task_id="critic", objective="检查硬约束、证据缺口和反例", agent=AgentName.RISK_CRITIC, dependencies=["preference"], completion_criteria=["关键风险已列出"]),
        ], missing_information=["缺少可验证的外部证据"], replan_conditions=["关键资料不可用"])

    def autonomous_plan(self, request: DecisionRequest, skills: list[dict[str, Any]]) -> AutonomousPlan:
        """模型不可用时生成最小、安全且不假定外部事实的通用计划。"""
        fallback_skill = next((item.get("name") for item in skills if item.get("name") == "risk-debate-moderator"), None)
        return AutonomousPlan(
            decision_type=request.decision_type or DecisionType.GENERAL,
            skill_name=fallback_skill,
            planning_summary="模型不可用，已采用本地保守分析；不会把未核验的外部信息当作事实。",
            plan=self.plan(request, request.decision_type or DecisionType.GENERAL),
        )

    def react(self, task: TaskSpec, target_id: str | None = None) -> ReActDecision:
        """离线时只结束无工具判断，避免用猜测的参数访问外部 MCP 服务。"""
        return ReActDecision(
            action="finish", public_summary="模型不可用，当前专家不调用外部工具并保留信息缺口。",
            target_resolution=TargetResolution(
                target_id=target_id or f"{task.task_id}-primary", status="blocked",
                summary="模型不可用，无法对当前信息目标作出外部核验。",
                missing_information=list(task.completion_criteria),
            ),
        )

    def information_plan(self, task: TaskSpec) -> ExpertInformationPlan:
        """离线时以单个保守目标承接任务，避免编造细分外部资料。"""
        return ExpertInformationPlan(targets=[InformationTarget(
            target_id=f"{task.task_id}-primary", objective=task.objective,
            completion_criteria=list(task.completion_criteria),
        )])

    def judge(self, request: DecisionRequest, *, evidence: list[Evidence] | None = None,
              memory: MemoryContext | None = None, dimensions: list[str] | None = None) -> DecisionReport:
        evidence = evidence or []
        candidates = request.candidates or self._extract_candidates(request.query) or ["需要补充候选项"]
        rejected = [candidate for candidate in candidates if self._violates(candidate, request.constraints)]
        eligible = [candidate for candidate in candidates if candidate not in rejected]
        scores = {candidate: self._score(candidate, request.preferences, memory) for candidate in eligible}
        recommendation = max(eligible, key=lambda item: (scores[item], -candidates.index(item))) if eligible else "没有满足硬约束的候选项"
        confirmed = [f"{item.claim}: {item.value}" for item in evidence if item.status.value == "confirmed"]
        external = [f"{item.claim}: {item.value}（{item.source or '来源未标明'}）" for item in evidence if item.source]
        uncertainties: list[str] = []
        if not evidence:
            uncertainties.append("没有可验证的外部 Evidence；实时价格、地点、天气、市场和网页事实均未确认")
        if not request.candidates:
            uncertainties.append("候选项未以结构化字段提供，已仅按问题文本进行有限解析")
        confidence = min(0.45, 0.25 + 0.05 * len(confirmed) + (0.05 if request.preferences else 0))
        return DecisionReport(
            recommended_option=recommendation, confidence=confidence,
            confirmed_facts=confirmed, external_views=external,
            inferences=[f"在已提供偏好和硬约束下，{recommendation} 的本地规则分数最高"] if eligible else [],
            preference_matches=[item for item in request.preferences if recommendation.casefold() in item.casefold() or item],
            uncertainties=uncertainties, rejected_options=rejected,
            tradeoffs=[f"{item}: 仍需按 {', '.join(dimensions or ['成本', '收益', '风险'])} 补齐可比较资料" for item in eligible],
            risks=["离线降级结果不能替代实时信息核验"],
            next_verification_steps=["补充每个候选项的同口径数据并通过只读来源二次核验"],
            analysis_mode=AnalysisMode.DETERMINISTIC_FALLBACK,
            fallback_reason="模型服务不可用或输出不符合结构化契约",
        )

    @staticmethod
    def _score(candidate: str, preferences: list[str], memory: MemoryContext | None) -> float:
        token = candidate.casefold()
        score = sum(1.0 for preference in preferences if token in preference.casefold())
        if memory:
            for profile in memory.profile_memories:
                if token in str(profile.value).casefold():
                    score += profile.importance * profile.confidence
        return score

    @staticmethod
    def _violates(candidate: str, constraints: list[str]) -> bool:
        token = candidate.casefold()
        negative = ("不满足", "不符合", "超过", "禁止", "淘汰", "exclude", "violates", "over budget")
        return any(token in constraint.casefold() and any(term in constraint.casefold() for term in negative) for constraint in constraints)

    @staticmethod
    def _extract_candidates(query: str) -> list[str]:
        normalized = re.sub(r"怎么选|如何选|谁更适合|值得吗|哪个好|比较", "", query, flags=re.IGNORECASE)
        parts = re.split(r"\s*(?:还是|或| vs\.? |VS|和)\s*", normalized)
        return [part.strip(" ，。?？") for part in parts if part.strip(" ，。?？")][:5] if len(parts) > 1 else []


class ModelAdapter:
    def __init__(self, settings: Settings, client: Any = None, *, trace_sink: Callable[[dict[str, Any]], Any] | None = None,
                 reasoner: DeterministicReasoner | None = None) -> None:
        self.settings = settings
        self.client = client if client is not None else self._build_client()
        self.trace_sink = trace_sink
        self.reasoner = reasoner or DeterministicReasoner()
        self.fallback_events: list[dict[str, Any]] = []

    def _build_client(self) -> Any:
        if not self.settings.llm_model_id or not self.settings.llm_api_key:
            return None
        try:
            from openai import AsyncOpenAI
            return AsyncOpenAI(
                api_key=self.settings.llm_api_key.get_secret_value(),
                base_url=self.settings.llm_base_url,
                timeout=self.settings.request_timeout_seconds,
            )
        except Exception:
            return None

    async def structured(self, system: str, payload: BaseModel | dict[str, Any], schema: type[T]) -> T:
        if self.client is None or not self.settings.llm_model_id:
            raise RuntimeError("LLM is not configured")
        body = payload.model_dump_json() if isinstance(payload, BaseModel) else json.dumps(payload, ensure_ascii=False, default=str)
        content = await self._complete_json(system, body)
        return schema.model_validate_json(content)

    async def _complete_json(self, system: str, body: str) -> str:
        """向 OpenAI 兼容服务请求 JSON 对象，并保留原始文本供上层结构校验与修正。"""
        if self.client is None or not self.settings.llm_model_id:
            raise RuntimeError("LLM is not configured")
        response = await self.client.chat.completions.create(
            model=self.settings.llm_model_id,
            messages=[{"role": "system", "content": f"{system}\n\n用户信息边界：{USER_INFORMATION_BOUNDARY}"}, {"role": "user", "content": body}],
            response_format={"type": "json_object"},
            timeout=self.settings.request_timeout_seconds,
        )
        content = response.choices[0].message.content
        if not content:
            raise ValueError("model returned empty structured content")
        return content

    async def _structured_with_repair(self, *, operation: str, system: str,
                                      payload: BaseModel | dict[str, Any],
                                      schema: type[T]) -> T:
        """以完整 Schema 和最多五次校验修正请求模型，失败后把异常交给调用方降级。"""
        # Schema 与正文一同发送，使模型能在首次生成时看见字段、枚举和嵌套约束。
        output_schema = schema.model_json_schema()
        system_with_schema = (
            f"{system}\n\n{STRUCTURED_OUTPUT_RULES}\n"
            f"输出契约名称：{schema.__name__}。完整 JSON Schema：\n"
            f"{json.dumps(output_schema, ensure_ascii=False)}"
        )
        base_payload = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else dict(payload)
        invalid_output = ""
        validation_error = ""
        # 每次失败把 Pydantic 的实际错误和上次原文回传模型；五次后才让调用方执行确定性降级。
        for attempt in range(1, 6):
            current_payload = dict(base_payload)
            if validation_error:
                current_payload["repair_instruction"] = "上一次输出未通过结构化校验。仅修正并返回完整 JSON 对象。"
                current_payload["previous_validation_error"] = validation_error
                current_payload["previous_invalid_output"] = invalid_output[:12000]
            content = await self._complete_json(
                system_with_schema,
                json.dumps(current_payload, ensure_ascii=False, default=str),
            )
            try:
                return schema.model_validate_json(content)
            except ValidationError as error:
                invalid_output = content
                validation_error = json.dumps(error.errors(include_url=False), ensure_ascii=False, default=str)
                self._record_structured_retry(operation, attempt, validation_error)
        raise ValueError(f"{schema.__name__} validation failed after 5 repair attempts: {validation_error[:800]}")

    async def judge_or_fallback(self, request: DecisionRequest, *, evidence: list[Evidence] | None = None,
                                memory: MemoryContext | None = None, dimensions: list[str] | None = None,
                                execution_context: dict[str, Any] | None = None) -> DecisionReport:
        try:
            report = await self._structured_with_repair(
                operation="judge",
                system="你是决策裁判。只使用载荷中的证据和总控进度摘要，严格按 JSON Schema 输出，不编造外部事实。"
                "必须区分已完成、证据不足与未验证的信息；不得把任务未完成误写为已确认。"
                f"{EVIDENCE_SUFFICIENCY_GUIDANCE}",
                payload={"request": request.model_dump(mode="json"), "evidence": [item.model_dump(mode="json") for item in (evidence or [])],
                         "memory": memory.model_dump(mode="json") if memory else {}, "dimensions": dimensions or [],
                         "execution_context": execution_context or {}},
                schema=DecisionReport,
            )
            return report.model_copy(update={"analysis_mode": AnalysisMode.MODEL, "fallback_reason": None})
        except Exception as error:
            self._fallback("judge", error)
            return self.reasoner.judge(request, evidence=evidence, memory=memory, dimensions=dimensions)

    async def plan_or_fallback(self, request: DecisionRequest, decision_type: DecisionType) -> ExecutionPlan:
        try:
            return await self.structured(
                "你是计划器。只输出满足 ExecutionPlan schema 的 JSON。",
                request, ExecutionPlan,
            )
        except Exception as error:
            self._fallback("plan", error)
            return self.reasoner.plan(request, decision_type)

    async def autonomous_plan_or_fallback(self, request: DecisionRequest, *, skills: list[dict[str, Any]],
                                          tools: list[Any], memory: MemoryContext,
                                          execution_context: dict[str, Any] | None = None) -> AutonomousPlan:
        """让总控模型从完整目录中选择领域、Skill、专家和任务 DAG。"""
        allowed_agents = [item.value for item in AgentName if item not in {AgentName.PLANNER, AgentName.JUDGE, AgentName.DEBATE_MODERATOR}]
        serialized_tools = [item.model_dump(mode="json") if hasattr(item, "model_dump") else item for item in tools]
        tool_permissions: dict[str, list[dict[str, Any]]] = {agent: [] for agent in allowed_agents}
        for tool in serialized_tools:
            if not isinstance(tool, dict):
                continue
            for agent in tool.get("allowed_agents", []):
                if agent in tool_permissions:
                    tool_permissions[agent].append({
                        "name": tool.get("name"), "capability": tool.get("capability"),
                        "description": tool.get("description"), "input_schema": tool.get("input_schema", {}),
                    })
        output_schema = AutonomousPlan.model_json_schema()
        system = (
            "你是个人决策系统的总控 Agent。依据用户请求、记忆、候选 Skill、专家权限与 MCP 工具目录制定执行计划。"
            "Skill 的建议专家、工具、SOP 与维度只供参考，不得按关键词硬匹配。"
            "只能选择载荷中出现的 Skill、专家和工具能力；任务必须可审计，规划摘要只说明公开依据，不能输出私有思维链。"
            f"{EVIDENCE_SUFFICIENCY_GUIDANCE}"
            "若载荷提供 execution_context，必须复用其中成功证据、已完成任务和 information_coverage 的最新 complete 状态，"
            "计划仅包含尚未满足的缺口；不得重新安排已完成任务或已完整覆盖的信息目标。\n\n"
            "当 execution_context.unmet_gaps 非空时，plan.tasks 必须至少包含一项可执行的新任务或恢复任务；"
            "每个任务的 objective 必须明确写出要补齐的缺口、使用哪个允许能力或专家分析、以及完成条件。"
            "不得用空任务数组、泛泛的‘继续调查’或重复已完成任务冒充重规划。\n\n"
            f"{STRUCTURED_OUTPUT_RULES}。该对象必须严格符合以下 JSON Schema：\n"
            f"{json.dumps(output_schema, ensure_ascii=False)}\n\n{AUTONOMOUS_PLAN_FIELD_CONTRACT}\n\n"
            f"允许的决策类型：{json.dumps([item.value for item in DecisionType], ensure_ascii=False)}\n"
            f"允许的专家 Agent 与各自当前可调用工具：{json.dumps(tool_permissions, ensure_ascii=False)}"
        )
        base_payload: dict[str, Any] = {
            "request": request.model_dump(mode="json"), "memory": memory.model_dump(mode="json"),
            "skills": skills, "tools": serialized_tools, "available_agents": allowed_agents,
            "execution_context": execution_context or {},
        }
        invalid_output = ""
        validation_error = ""
        try:
            for attempt in range(1, 6):
                payload = dict(base_payload)
                if validation_error:
                    payload["repair_instruction"] = "上一次输出未通过结构化校验。仅修正并返回完整 JSON 对象。"
                    payload["previous_validation_error"] = validation_error
                    payload["previous_invalid_output"] = invalid_output[:12000]
                content = await self._complete_json(system, json.dumps(payload, ensure_ascii=False, default=str))
                try:
                    return AutonomousPlan.model_validate_json(content)
                except ValidationError as error:
                    invalid_output = content
                    validation_error = json.dumps(error.errors(include_url=False), ensure_ascii=False, default=str)
                    self._record_structured_retry("autonomous_plan", attempt, validation_error)
            raise ValueError(f"AutonomousPlan validation failed after 5 repair attempts: {validation_error[:800]}")
        except Exception as error:
            self._fallback("autonomous_plan", error)
            return self.reasoner.autonomous_plan(request, skills)

    async def react_or_fallback(self, *, task: TaskSpec, request: DecisionRequest,
                                memory: MemoryContext, observations: list[dict[str, Any]],
                                tools: list[Any], remaining_calls: int,
                                remaining_binding_calls: int = 3,
                                execution_context: dict[str, Any] | None = None) -> ReActDecision:
        """让专家模型在每轮根据真实工具 Schema 和观察结果选择下一步受控动作。"""
        try:
            tool_reference = self._tool_reference(tools)
            # 将运行时状态整理成稳定字段，而不是让模型从零散 Trace 中猜测目标和覆盖进度。
            normalized_context = self._react_context_payload(task, request, memory, observations, execution_context)
            return await self._structured_with_repair(
                operation="react",
                system="你是受限 ReAct 专家。只依据任务、用户请求、记忆、已观察到的结果和允许工具行动。"
                "调用工具时 action 必须为 call_tool，且 tool_name 必须逐字选择 allowed_tools 中的一个 name；不得输出 capability。"
                "调用工具时必须完全符合该 tool_name 对应 input_schema；网页抓取工具通常需要 URL，不得把搜索 query 当 URL。"
                "必须阅读 execution_context.react_context。它只包含用户问题、Task、当前 target、当前 target completion_criteria、"
                "当前 target observations、latest_summary、coverage、missing_information、剩余额度及必要记忆和用户约束。"
                "不得根据或索取其他 target 的摘要、缺口、状态或证据；这些资料不属于当前行动的上下文。"
                "当选择 finish 时，必须填写 target_resolution：target_id 必须等于当前信息目标，status 只能为 complete、partial 或 blocked，"
                "并说明公开摘要与仍缺信息。target_resolution.evidence_refs 可引用当前任务此前观察或证据账本中的 call_id；"
                "不得为完成状态重复调用工具。finish 不是提前结束接口：只有专用 Settlement 将 target_complete 标为 true 才会结束当前目标。"
                f"{EVIDENCE_SUFFICIENCY_GUIDANCE}"
                "每次工具成功且 supports_current_target=true 后，系统会立即交给专用结算节点逐项检查完成条件；"
                "partial 只能继续搜索。你无需也不得输出 coverage_updates。"
                "失败时优先改参数、换工具或请求重规划；同工具同参数若此前失败、超时或不可用，不得再次调用；信息显著不足时可请求最多三个用户补充字段。"
                "public_summary 只能描述将做什么及公开依据，不得暴露私有思维链。",
                payload={
                    "task": task.model_dump(mode="json"), "request": request.model_dump(mode="json"),
                    "memory": memory.model_dump(mode="json"),
                    "observations": normalized_context.get("current_task_history", []),
                    "allowed_tools": tool_reference,
                    "remaining_calls": remaining_calls,
                    "remaining_binding_calls": remaining_binding_calls,
                    "execution_context": normalized_context,
                    "react_validation_error": normalized_context.get("react_validation_error"),
                },
                schema=ReActDecision,
            )
        except Exception as error:
            self._fallback("react", error)
            active_target = (execution_context or {}).get("active_information_target", {})
            return self.reasoner.react(task, str(active_target.get("target_id") or f"{task.task_id}-primary"))

    async def settle_current_target_after_observation_or_none(self, *, current_target: dict[str, Any],
                                                               target_observations: list[dict[str, Any]],
                                                               existing_coverage: dict[str, Any]) -> TargetSettlementSubmission | None:
        """每条语义可用工具观察都立即进入专用结算，不依赖下一轮 ReAct 是否主动提交状态。"""
        if self.client is None or not self.settings.llm_model_id:
            return None
        try:
            # 每条可用观察只触发一次独立结算，不属于 ReAct 的五次 JSON 修正循环。
            schema = TargetSettlementSubmission.model_json_schema()
            system = (
                "你是信息目标结算器。只根据当前信息目标、明确完成条件、该目标已支持的观察和已有覆盖状态，"
                "逐条判断 criteria 是否满足，输出 coverage_status、missing_information、target_complete 和公开 summary。"
                "不要调用工具、不要规划、不要输出 action、不要编造用户或外部信息。"
                "只有每一条完成条件均被当前目标的观察支持时，target_complete 才能为 true 且 coverage_status 才能为 full；"
                "否则必须为 partial，并明确每项未满足条件的 missing。partial 表示继续搜索，绝不结束当前目标。"
                f"\n\n{STRUCTURED_OUTPUT_RULES}\n"
                "输出契约名称：TargetSettlementSubmission。完整 JSON Schema：\n"
                f"{json.dumps(schema, ensure_ascii=False)}"
            )
            sanitized_target = {
                key: current_target[key] for key in ("target_id", "objective", "completion_criteria", "status", "latest_summary")
                if key in current_target
            }
            payload = {
                "current_target": sanitized_target,
                "current_target_observations": target_observations,
                "existing_coverage": existing_coverage,
            }
            content = await self._complete_json(system, json.dumps(payload, ensure_ascii=False, default=str))
            return TargetSettlementSubmission.model_validate_json(content)
        except Exception as error:
            # 补交失败不把原本成功的工具观察改写为失败，框架仍会按既有流程处理后续动作。
            self._fallback("target_settlement_submission", error)
            return None

    async def assess_tool_binding_or_fallback(self, *, request: DecisionRequest, task: TaskSpec,
                                               target: dict[str, Any], tool: dict[str, Any],
                                               arguments: dict[str, Any]) -> ToolBindingAssessment:
        """在调用 MCP 前验证参数是否实际服务当前信息目标。"""
        try:
            return await self._structured_with_repair(
                operation="tool_binding_assessment",
                system=(
                    "你是 MCP 工具调用前的语义绑定核验器。只根据用户约束、当前任务、当前信息目标及其完成条件、"
                    "所选工具和参数判断这组参数是否直接服务当前信息目标。"
                    "参数符合 JSON Schema、工具被允许或任务提到相近实体，都不代表已经绑定。"
                    "若参数查询的是另一城市、另一候选项、另一日期或另一资料目标，bound 必须为 false，并给出可公开的简短修正原因。"
                    "不得看到、推断或提及其他信息目标，不得编造外部事实或私有思维链。"
                ),
                payload={
                    "user_question": request.query,
                    "constraints": request.constraints,
                    "task": {"task_id": task.task_id, "objective": task.objective},
                    "current_target": target,
                    "selected_tool": tool,
                    "arguments": arguments,
                },
                schema=ToolBindingAssessment,
            )
        except Exception as error:
            self._fallback("tool_binding_assessment", error)
            return ToolBindingAssessment(
                bound=False,
                reason="调用前语义绑定模型不可用，无法安全确认工具参数服务当前信息目标。",
            )

    async def assess_observation_or_fallback(self, *, request: DecisionRequest, task: TaskSpec,
                                             target: dict[str, Any], observation: ToolObservation) -> ObservationAssessment:
        """用模型判断工具内容是否真正支撑当前目标，绝不以传输状态或关键词代替语义判断。"""
        try:
            return await self._structured_with_repair(
                operation="observation_assessment",
                system=(
                    "你是工具观察语义核验器。仅根据用户问题、当前任务、当前信息目标、完成条件、工具参数和工具返回内容，"
                    "判断该内容是否实际支撑当前目标。不得按关键词或传输状态猜测相关性：MCP 成功、HTTP 成功或文本提到相似地名都不等于相关。"
                    "输出 relevant（足以支撑）、partial（相关、可作合理推断但不完整）、irrelevant（内容与当前目标完全无关且无法作合理推断）"
                    "或 unverifiable（内容不足以判断）。除完全无关外，宁可保守标记为 partial 并说明缺口，不要因资料不够精确而标记 irrelevant。"
                    "如果资料不直接支持当前目标，supports_current_target 必须为 false；related_target_ids 只可作为旁路 metadata，"
                    "不得把它用于任何其他目标的覆盖更新。coverage_contribution 只能是 partial 或 full，且只描述当前目标的覆盖贡献。"
                    "summary 只陈述公开的核验依据；不得编造外部事实或私有思维链。"
                ),
                payload={
                    "user_question": request.query, "task": task.model_dump(mode="json"), "target": target,
                    "completion_criteria": target.get("completion_criteria", task.completion_criteria),
                    "tool_observation": observation.model_dump(mode="json"),
                },
                schema=ObservationAssessment,
            )
        except Exception as error:
            self._fallback("observation_assessment", error)
            return ObservationAssessment(
                relevance=ObservationRelevance.UNVERIFIABLE,
                summary="模型不可用，无法核验本次工具返回是否与当前信息目标相关。",
                missing_information=list(target.get("completion_criteria", task.completion_criteria)),
            )

    async def resolve_general_task_or_fallback(self, *, task: TaskSpec, request: DecisionRequest,
                                               memory: MemoryContext, execution_context: dict[str, Any]) -> GeneralTaskResolution:
        """让通用 Agent 一次性完成综合、归纳或比较类任务。"""
        try:
            return await self._structured_with_repair(
                operation="general_task_resolution",
                system=(
                    "你是通用决策执行 Agent。只依据给定任务、用户约束、记忆和任务范围内证据，完成一次公开的综合、比较或归纳。"
                    "不得调用工具、不得编造外部事实或私有思维链；证据不足时在 uncertainties 中说明，并使用 completed_with_gaps 或 blocked。"
                ),
                payload={
                    "task": task.model_dump(mode="json"), "request": request.model_dump(mode="json"),
                    "memory": memory.model_dump(mode="json"), "task_execution_context": execution_context,
                },
                schema=GeneralTaskResolution,
            )
        except Exception as error:
            self._fallback("general_task_resolution", error)
            return GeneralTaskResolution(
                summary="通用综合模型不可用，未将未核验资料当作结论。",
                findings=[], uncertainties=["通用综合模型不可用"], completion_status="blocked",
            )

    async def replan_decision_or_fallback(self, *, request: DecisionRequest,
                                          execution_context: dict[str, Any]) -> ReplanDecision:
        """由总控判定未完成资料是否会实质改变结论且能否生成补救任务。"""
        try:
            return await self._structured_with_repair(
                operation="replan_decision",
                system=(
                    "你是决策总控的重规划判断器。依据执行上下文判断是否需要补充任务。"
                    "只有缺口可能实质改变推荐、硬约束结论或关键风险，并且确有允许专家和工具可执行补救时，should_replan 才能为 true。"
                    "普通细节缺口、已标记 blocked/partial 但不改变结论的资料，应保留为不确定性并进入核验或最终判断。"
                    "不得只因某个任务有 uncertainties 或工具失败就重规划；不得编造用户信息、工具能力或外部事实。"
                ),
                payload={"request": request.model_dump(mode="json"), "execution_context": execution_context},
                schema=ReplanDecision,
            )
        except Exception as error:
            self._fallback("replan_decision", error)
            return ReplanDecision(
                should_replan=False,
                reason="模型不可用，保留现有缺口并进入核验或最终判断，不自动重复执行任务。",
                critical_gaps=[], can_execute_remedy=False,
            )

    async def information_plan_or_fallback(self, *, task: TaskSpec, request: DecisionRequest,
                                           memory: MemoryContext, tools: list[Any],
                                           execution_context: dict[str, Any]) -> ExpertInformationPlan:
        """让专家在 ReAct 前将任务拆成最多五项可审计信息目标。"""
        try:
            tool_reference = self._tool_reference(tools)
            return await self._structured_with_repair(
                operation="expert_information_plan",
                system=(
                    "你是专家任务规划器。仅根据用户问题、当前任务、记忆、已有信息和允许工具，输出 ExpertInformationPlan JSON。"
                    "最多规划五个信息目标；每项必须是完成当前任务所必需、可观察、可独立完成的资料目标。"
                    "不要按行业关键词套模板，不要编造用户信息或外部事实。"
                    "target_id 必须稳定、仅用小写字母数字和 . _ - :；同一目标后续 ReAct 必须复用该 ID。"
                    "每个目标的 completion_criteria 要说明何种已观察资料才算足够；不要把最终推荐本身作为资料目标。"
                    "优先创建最少、互不重复的目标；除硬约束外，不要把同一体验或偏好维度拆成大量精确数字、逐地点或逐时段验证。"
                    f"{EVIDENCE_SUFFICIENCY_GUIDANCE}"
                ),
                payload={
                    "task": task.model_dump(mode="json"), "request": request.model_dump(mode="json"),
                    "memory": memory.model_dump(mode="json"), "allowed_tools": tool_reference,
                    "execution_context": execution_context,
                },
                schema=ExpertInformationPlan,
            )
        except Exception as error:
            self._fallback("expert_information_plan", error)
            return self.reasoner.information_plan(task)

    async def evidence_relationship_or_fallback(self, *, left: Evidence, right: Evidence) -> EvidenceRelationship:
        """仅比较同一 scope_key 的候选资料；模型不可用时保守保留为未核验关系。"""
        try:
            return await self._structured_with_repair(
                operation="evidence_relationship",
                system=(
                    "你是证据关系判别器。仅比较载荷中同一 scope_key 的两条外部资料，输出 supports、complements、contradicts 或 uncertain。"
                    "文本不同不代表矛盾：日期片段、细节补充、精确度不同通常是 complements。"
                    "只有对同一事实给出不能同时成立的结论才可输出 contradicts；无法确定则输出 uncertain。"
                    "不得补充外部事实或输出私有思维链。"
                ),
                payload={"left": left.model_dump(mode="json"), "right": right.model_dump(mode="json")},
                schema=EvidenceRelationship,
            )
        except Exception as error:
            self._fallback("evidence_relationship", error)
            return EvidenceRelationship(relation="uncertain", summary="模型不可用，保留为未核验关系而不判定冲突。")

    async def summarize_tool_result(self, *, query: str, task_objective: str,
                                    tool_name: str, raw_result: str) -> str:
        """把任意长工具输出压缩为可展示的事实摘要；模型不可用时保守截断原文。"""
        fallback = raw_result[:1500]
        if self.client is None or not self.settings.llm_model_id:
            return fallback
        try:
            result = await self._structured_with_repair(
                operation="trace_summary",
                system="你是工具结果摘要器。只根据给定的用户问题、任务和工具原文，提炼供用户查看的关键结果。"
                "不得补充、猜测或改写未出现的事实；如果原文只是字段说明、错误信息或没有实际数据，必须明确说明。"
                "不输出私有思维链、建议或 Markdown。",
                payload={
                    "user_query": query, "task_objective": task_objective,
                    "tool_name": tool_name, "raw_result": raw_result[:12000],
                },
                schema=TraceSummary,
            )
            return result.summary
        except Exception:
            return fallback

    @staticmethod
    def _tool_reference(tools: list[Any]) -> list[dict[str, Any]]:
        """压缩专家可用工具为能力、简易用途和参数 Schema，避免模型误用其他 Agent 的工具。"""
        references: list[dict[str, Any]] = []
        for item in tools:
            raw = item.model_dump(mode="json") if hasattr(item, "model_dump") else item
            if not isinstance(raw, dict):
                continue
            capability, name = raw.get("capability"), raw.get("name")
            if not isinstance(capability, str) or not isinstance(name, str):
                continue
            references.append({
                "capability": capability,
                "name": name,
                "purpose": raw.get("description", ""),
                "input_schema": raw.get("input_schema", {}),
            })
        return references

    @staticmethod
    def _react_context_payload(task: TaskSpec, request: DecisionRequest, memory: MemoryContext,
                               observations: list[dict[str, Any]],
                               execution_context: dict[str, Any] | None) -> dict[str, Any]:
        """补齐 ReAct 固定上下文合同，使任何调用方都向模型提供同一组公开字段。"""
        normalized = dict(execution_context or {})
        existing = normalized.get("react_context")
        react_context = dict(existing) if isinstance(existing, dict) else {}
        last = observations[-1] if observations else None
        active_target = dict(normalized.get("active_information_target", {}))
        target_id = active_target.get("target_id")
        target_coverage = normalized.get("information_coverage", {}).get(target_id, {}) if target_id else {}
        react_context.setdefault("用户问题", request.query)
        react_context.setdefault("Task", {"task_id": task.task_id, "objective": task.objective})
        react_context.setdefault("当前 target", active_target)
        react_context.setdefault("当前 target completion_criteria", active_target.get("completion_criteria", task.completion_criteria))
        prompt_history = [
            {
                **{"tool_name": item.get("tool_name"), "arguments": item.get("arguments", {}), "status": item.get("status")},
                **({"result_summary": item["result_summary"]} if item.get("result_summary") else {}),
                **({"semantic_status": item["semantic_status"]} if item.get("semantic_status") else {}),
                **({"semantic_summary": item["semantic_summary"]} if item.get("semantic_summary") else {}),
            }
            for item in observations
        ]
        react_context.setdefault("当前 target 已有 observations", prompt_history)
        react_context.setdefault("当前 target latest_summary", active_target.get("latest_summary"))
        react_context.setdefault("当前 target coverage", target_coverage)
        react_context.setdefault("当前 target missing_information", active_target.get("missing_information", active_target.get("completion_criteria", task.completion_criteria)))
        react_context.setdefault("相关记忆与 HITL 补充", {
            "memory": memory.model_dump(mode="json"), "hitl": request.context.get("hitl", {}),
        })
        if last and last.get("status") != "succeeded" and last.get("error"):
            react_context.setdefault("上一轮失败原因", last["error"])
        return {
            "active_information_target": active_target,
            "current_task_history": prompt_history,
            "react_context": react_context,
            "react_validation_error": normalized.get("react_validation_error"),
        }

    def reset_fallback_events(self) -> None:
        """每次新决策前清除前次请求留下的降级诊断。"""
        self.fallback_events = []

    def _record_structured_retry(self, operation: str, attempt: int, error: str) -> None:
        """记录公开的结构化修正事件，供工作流 Trace 展示而不泄漏模型原文。"""
        self.fallback_events.append({"mode": "structured_retry", "operation": operation, "attempt": attempt, "reason": error[:800]})

    async def extract_explicit_profile_signals(self, *, decision_type: DecisionType, chosen_reason: str) -> list[str]:
        """仅从用户亲自填写的选择理由提炼偏好；模型不可用时宁可不推断。"""
        if not chosen_reason.strip():
            return []
        try:
            extracted = await self.structured(
                "从用户明确填写的选择理由中提炼稳定偏好。只输出 signals，每项格式必须为 "
                "explicit:<decision_type>:<lowercase_key>=<value>；不得把模型建议或外部事实写成偏好。"
                "没有可靠偏好则输出空列表。",
                {"decision_type": decision_type.value, "chosen_reason": chosen_reason}, ProfileSignalExtraction,
            )
            prefix = f"explicit:{decision_type.value}:"
            return [signal for signal in extracted.signals if signal.startswith(prefix)]
        except Exception as error:
            self._fallback("profile_signal_extraction", error)
            return []

    async def extract_user_profile_signals(self, *, texts: list[str],
                                           temporal_context: dict[str, Any]) -> list[str]:
        """仅从用户亲自提供的文本提取长期画像；失败时宁可不创建任何画像。"""
        user_texts = [text.strip() for text in texts if isinstance(text, str) and text.strip()]
        if not user_texts:
            return []
        try:
            extracted = await self.structured(
                "你是用户画像提取器。仅从载荷中的用户原文提取用户明确陈述、且适合长期保存的画像："
                "职业、专业、技能、兴趣爱好、常住或偏好地区、生日、出生年份等。"
                "不得根据问题主题、模型建议、外部事实或常识猜测；没有明确画像则输出空列表。"
                "只输出 signals，每项必须为 explicit:user_profile:<lowercase_underscore_key>=<value>。"
                "年龄不得以 age 保存：若仅有年龄没有生日，按 temporal_context.reference_date 转为 birth_year_range，"
                "格式 YYYY-YYYY；若有明确生日，可以同时保存 birthday 与 birth_year。",
                {"user_texts": user_texts, "temporal_context": temporal_context},
                ProfileSignalExtraction,
            )
            reference_date = temporal_context.get("reference_date")
            return self._normalize_user_profile_signals(extracted.signals, reference_date if isinstance(reference_date, str) else None)
        except Exception as error:
            self._fallback("user_profile_extraction", error)
            return []

    @staticmethod
    def _normalize_user_profile_signals(signals: list[str], reference_date: str | None) -> list[str]:
        """过滤越界模型输出，并把意外返回的年龄统一改为保守出生年份范围。"""
        normalized: list[str] = []
        valid = re.compile(r"^explicit:user_profile:(?P<key>[a-z][a-z0-9_]{0,63})=(?P<value>[^:\n]+)$")
        for signal in signals:
            if not isinstance(signal, str):
                continue
            match = valid.fullmatch(signal.strip())
            if match is None:
                continue
            key, value = match.group("key"), match.group("value").strip()
            if not value:
                continue
            if key == "age":
                if reference_date is None or not value.isdigit():
                    continue
                age = int(value)
                if not 0 < age < 130:
                    continue
                signal = f"explicit:user_profile:birth_year_range={birth_year_range_from_age(age, reference_date)}"
            if signal not in normalized:
                normalized.append(signal)
        return normalized

    def _fallback(self, operation: str, error: Exception) -> None:
        event = {"mode": "deterministic_fallback", "operation": operation, "reason": type(error).__name__}
        self.fallback_events.append(event)
        if self.trace_sink:
            self.trace_sink(event)
