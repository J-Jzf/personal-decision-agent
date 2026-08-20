# Personal Decision Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Build a local FastAPI multi-agent decision system with safe MCP research, three-level memory, decision archiving, feedback, and retrospective analysis.

**Architecture:** FastAPI assembles a LangGraph decision workflow and typed agents. SQLite is the authority for structured state; embedded Qdrant is a rebuildable episode similarity index. The MCP Gateway dynamically discovers local stdio MCP tools, enforces a read-only policy, and converts failures into typed observations. OpenAI-compatible calls have a deterministic local fallback.

**Tech Stack:** Python 3.11+, FastAPI, Uvicorn, Pydantic v2, LangGraph, OpenAI SDK, MCP Python SDK, SQLite, Qdrant local mode, Pytest, HTTPX.

**Spec:** docs/superpowers/specs/2026-08-18-personal-decision-agent-design.md

## Global Constraints

- Python modules use Pydantic models at all component boundaries.
- SQLite at var/personal_decision.db is the source of truth; Qdrant at var/qdrant is a derived, rebuildable episode index.
- Agents never access databases, MCP sessions, SDKs, shells, or local files directly.
- Gateway allows only registered, read-only capabilities assigned to the requesting Agent.
- The local reasoning fallback never invents external facts and caps confidence when evidence is absent.
- Reports label confirmed facts, external views, inference, personal preferences, and uncertainty.
- The application performs no real-world write action, trade, purchase, booking, or offer acceptance.
- Tests run without model credentials, network, or real MCP services.

---

## File structure

~~~text
app/config.py                 Settings and runtime paths
app/container.py              Dependency assembly
app/api/routes.py             HTTP endpoints
models/contracts.py           Typed messages and reports
memory/database.py            SQLite schema and transaction access
memory/repositories.py        Archive, working, episode, profile, trace stores
memory/vector_index.py        Embedded Qdrant and deterministic embedding
memory/manager.py             Single memory access boundary and reflection
skills/registry.py            SKILL.md parser and validator
mcp/policy.py                 Capability, action, and Agent permission policy
mcp/registry.py               Capability-to-discovered-tool mapping
mcp/gateway.py                Safe calls, retries, normalization, audit logs
llm/adapter.py                OpenAI-compatible structured calls and fallback
agents/*.py                   Supervisor, planner, experts, critic, judge, debate
evidence/pool.py              Evidence dedupe and conflict tracking
graph/decision_graph.py       LangGraph workflow, execution, replan routing
app/main.py                   FastAPI application factory
main.py                       Command-line server entrypoint
skills/*/SKILL.md             Eight executable decision SOPs
tests/*.py                    Offline unit, API, and acceptance coverage
README.md                     Operational and architectural guide
~~~

### Task 1: Bootstrap configuration and typed contracts

**Files:**
- Create: requirements.txt, .env.example, app/__init__.py, app/config.py, models/__init__.py, models/contracts.py, tests/test_contracts.py

**Interfaces:**
- Produces Settings, DecisionType, WorkflowStatus, TaskSpec, ExecutionPlan, Evidence, ToolObservation, AgentResult, DecisionReport, and API payload models.

- [ ] **Step 1: Write the failing contract test**

~~~python
def test_report_requires_explicit_evidence_categories():
    from models.contracts import DecisionReport

    report = DecisionReport(recommended_option="A", confidence=0.4)
    assert report.confirmed_facts == []
    assert report.uncertainties == []
~~~

- [ ] **Step 2: Run the test to verify the missing-module failure**

Run: pytest tests/test_contracts.py::test_report_requires_explicit_evidence_categories -v

Expected: FAIL because models.contracts is absent.

- [ ] **Step 3: Implement settings and Pydantic contracts**

~~~python
class DecisionReport(BaseModel):
    recommended_option: str
    confidence: float = Field(ge=0, le=1)
    confirmed_facts: list[str] = Field(default_factory=list)
    external_views: list[str] = Field(default_factory=list)
    inferences: list[str] = Field(default_factory=list)
    preference_matches: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
~~~

Use pydantic-settings to load LLM_MODEL_ID, LLM_API_KEY, LLM_BASE_URL, database paths, timeout, and MCP command JSON. Add all enum/model validation required by the spec.

- [ ] **Step 4: Run contract tests**

Run: pytest tests/test_contracts.py -v

Expected: PASS for report defaults, enum validation, plan dependencies, and evidence status validation.

- [ ] **Step 5: Commit the typed bootstrap**

~~~powershell
git add requirements.txt .env.example app models tests/test_contracts.py
git commit -m "feat: add typed decision contracts"
~~~

### Task 2: Implement SQLite persistence and trace repositories

**Files:**
- Create: memory/__init__.py, memory/database.py, memory/repositories.py, tests/test_repositories.py

**Interfaces:**
- Consumes DecisionReport, Evidence, and AgentResult.
- Produces SQLiteDatabase.initialize(), ArchiveRepository.save/get/list(), WorkingMemoryRepository.save/get(), TraceRepository.record(), EvidenceRepository.add/list(), EpisodeRepository, ProfileRepository, FeedbackRepository, and RetrospectiveRepository.

- [ ] **Step 1: Write the failing archive round-trip test**

~~~python
def test_archive_round_trip(tmp_path):
    db = SQLiteDatabase(tmp_path / "decision.db")
    archives = ArchiveRepository(db)
    archives.save(ArchiveRecord(decision_id="D1", decision_type="product", query="A or B"))
    assert archives.get("D1").query == "A or B"
~~~

- [ ] **Step 2: Run the test to verify the missing repository failure**

Run: pytest tests/test_repositories.py::test_archive_round_trip -v

Expected: FAIL because SQLiteDatabase and ArchiveRepository are absent.

- [ ] **Step 3: Create the schema and repositories**

~~~sql
CREATE TABLE IF NOT EXISTS decision_archives (
  decision_id TEXT PRIMARY KEY, decision_type TEXT NOT NULL, query TEXT NOT NULL,
  status TEXT NOT NULL, candidates_json TEXT NOT NULL, constraints_json TEXT NOT NULL,
  preferences_json TEXT NOT NULL, plan_json TEXT NOT NULL, report_json TEXT NOT NULL,
  recommendation TEXT, confidence REAL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
~~~

Create all ten tables from the specification with parameterized SQL, JSON serialization, UTC ISO timestamps, and short transaction scopes.

- [ ] **Step 4: Run repository tests**

Run: pytest tests/test_repositories.py -v

Expected: PASS for archive, working state, evidence, tool calls, trace, feedback, and retrospective round trips.

- [ ] **Step 5: Commit persistence**

~~~powershell
git add memory tests/test_repositories.py
git commit -m "feat: persist decisions and workflow traces"
~~~

### Task 3: Build embedded Qdrant episode search and memory manager

**Files:**
- Create: memory/vector_index.py, memory/manager.py, tests/test_memory_manager.py

**Interfaces:**
- Consumes EpisodeRepository and ProfileRepository.
- Produces LocalEpisodeIndex.upsert/search(), MemoryManager.context_for(), MemoryManager.archive_completed(), and MemoryManager.record_feedback().

- [ ] **Step 1: Write a failing metadata-filtered retrieval test**

~~~python
def test_memory_manager_returns_only_matching_episodes(tmp_path):
    manager = build_memory_manager(tmp_path)
    manager.write_episode(episode("EP1", "job_offer", "杭州 AI 岗位"))
    manager.write_episode(episode("EP2", "travel", "杭州周末旅行"))
    found = manager.retrieve_episodes("杭州 Offer", "job_offer")
    assert [item.episode_id for item in found] == ["EP1"]
~~~

- [ ] **Step 2: Run the retrieval test to verify it fails**

Run: pytest tests/test_memory_manager.py::test_memory_manager_returns_only_matching_episodes -v

Expected: FAIL because MemoryManager is absent.

- [ ] **Step 3: Implement deterministic embedding, Qdrant local mode, and SQLite fallback**

~~~python
class LocalEpisodeIndex:
    def __init__(self, path: Path) -> None:
        self.client = QdrantClient(path=str(path))

    def search(self, query: str, decision_type: str, limit: int = 3) -> list[str]:
        return self._search_with_filter(query, decision_type, limit)
~~~

Derive a fixed-length normalized hash vector from Unicode tokens. Filter Qdrant payload by decision type. If Qdrant setup or search fails, rank SQLite summaries using type, token overlap, tags, and recency. Implement the explicit, repeated-inference, and conflicting-memory reflection rules exactly as defined in the spec.

- [ ] **Step 4: Run memory tests**

Run: pytest tests/test_memory_manager.py -v

Expected: PASS for retrieval, index fallback, explicit preference, repeated inferred preference, and conflict confidence reduction.

- [ ] **Step 5: Commit memory services**

~~~powershell
git add memory tests/test_memory_manager.py
git commit -m "feat: add hierarchical memory and episode search"
~~~

### Task 4: Add executable decision Skills and registry

**Files:**
- Create: skills/registry.py; the eight SKILL.md files under job_offer_evaluator, product_comparison, travel_destination_compare, portfolio_review, course_subscription_evaluator, risk_debate_moderator, evidence_verification, and decision_retrospective; tests/test_skill_registry.py

**Interfaces:**
- Produces SkillDefinition, SkillRegistry.load_all(), SkillRegistry.match(), and SkillRegistry.get().

- [ ] **Step 1: Write the failing Skill match test**

~~~python
def test_registry_matches_cross_city_offer():
    registry = SkillRegistry(Path("skills"))
    registry.load_all()
    assert registry.get("job-offer-evaluator").name == "job-offer-evaluator"
~~~

- [ ] **Step 2: Run the test to verify it fails**

Run: pytest tests/test_skill_registry.py::test_registry_discovers_all_eight_skill_definitions -v

Expected: FAIL because SkillRegistry is absent.

- [ ] **Step 3: Implement validation and all eight SOP files**

~~~python
REQUIRED_SKILL_FIELDS = {
    "name", "description", "recommended_agents", "recommended_tools",
    "analysis_dimensions",
    "workflow", "risk_checks", "completion_conditions", "output_schema",
}
~~~

Parse YAML front matter with yaml.safe_load and reject a definition missing any required field. Write complete SOP data with the agents, capabilities, dimensions, risk checks, and completion checks prescribed for each domain.

- [ ] **Step 4: Run Skill tests**

Run: pytest tests/test_skill_registry.py -v

Expected: PASS for loading eight Skills and rejecting an invalid definition.

- [ ] **Step 5: Commit Skills**

~~~powershell
git add skills tests/test_skill_registry.py
git commit -m "feat: add decision skill registry"
~~~

### Task 5: Implement secure MCP discovery, policy, and gateway

**Files:**
- Create: mcp/__init__.py, mcp/policy.py, mcp/registry.py, mcp/gateway.py, mcp/adapters.py, tests/test_mcp_gateway.py

**Interfaces:**
- Consumes Settings, ToolObservation, and TraceRepository.
- Produces MCPGateway.discover(), MCPGateway.call(), ToolRegistry.list_capabilities(), and ToolPolicy.authorize().

- [ ] **Step 1: Write failing policy and unavailable-tool tests**

~~~python
async def test_gateway_blocks_trade_and_returns_unavailable():
    gateway = fake_gateway({"market_data": FakeTool("ticker_info")})
    with pytest.raises(PermissionError):
        await gateway.call("financial_market", "market_data", {"action": "buy"})
    result = await gateway.call("location_lifestyle", "weather_forecast", {"city": "杭州"})
    assert result.status == "unavailable"
~~~

- [ ] **Step 2: Run the gateway test to verify failure**

Run: pytest tests/test_mcp_gateway.py::test_gateway_blocks_trade_and_returns_unavailable -v

Expected: FAIL because MCPGateway is absent.

- [ ] **Step 3: Implement mapping, discovery, normalization, and audit**

~~~python
FORBIDDEN_TERMS = {
    "execute", "shell", "install", "delete", "write", "send_money",
    "place_order", "buy", "sell", "book", "purchase", "submit", "accept_offer",
}

def authorize(agent: str, capability: str, arguments: dict[str, Any]) -> None:
    if capability not in AGENT_CAPABILITIES[agent] or contains_forbidden(arguments):
        raise PermissionError("MCP operation is not permitted")
~~~

Use MCP ClientSession for configured stdio commands. Discover schema, map internal capabilities, normalize return content, record each result, retry a transient timeout once, and return typed unavailable/error observations rather than raw session errors.

- [ ] **Step 4: Run gateway tests**

Run: pytest tests/test_mcp_gateway.py -v

Expected: PASS for discovery mapping, Agent whitelist, unsafe rejection, timeout retry, audit rows, and unavailable fallback.

- [ ] **Step 5: Commit MCP safety layer**

~~~powershell
git add mcp tests/test_mcp_gateway.py
git commit -m "feat: add safe MCP gateway"
~~~

### Task 6: Build model adapter and deterministic decision fallback

**Files:**
- Create: llm/__init__.py, llm/adapter.py, tests/test_model_adapter.py

**Interfaces:**
- Consumes Settings, ExecutionPlan, Evidence, and profile context.
- Produces ModelAdapter.structured(), DeterministicReasoner.plan(), and DeterministicReasoner.judge().

- [ ] **Step 1: Write a failing fallback confidence test**

~~~python
async def test_failed_model_uses_local_reasoner_without_external_claims():
    adapter = ModelAdapter(failing_client())
    report = await adapter.judge_or_fallback(problem_with_no_evidence())
    assert report.confidence <= 0.45
    assert report.confirmed_facts == []
    assert report.uncertainties
~~~

- [ ] **Step 2: Run the test to verify failure**

Run: pytest tests/test_model_adapter.py::test_failed_model_uses_local_reasoner_without_external_claims -v

Expected: FAIL because ModelAdapter is absent.

- [ ] **Step 3: Implement structured calls and weighted fallback**

~~~python
async def structured(self, system: str, payload: BaseModel, schema: type[T]) -> T:
    response = await self.client.chat.completions.create(
        model=self.settings.llm_model_id,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": payload.model_dump_json()}],
        response_format={"type": "json_object"},
    )
    return schema.model_validate_json(response.choices[0].message.content)
~~~

On missing configuration, transport error, malformed JSON, or timeout, write fallback mode into trace and apply hard-constraint rejection plus normalized Skill/profile weighted scoring. Add unavailable external facts to uncertainties and cap confidence at 0.45.

- [ ] **Step 4: Run adapter tests**

Run: pytest tests/test_model_adapter.py -v

Expected: PASS for valid structured reply, malformed reply fallback, unavailable configuration fallback, and hard-constraint elimination.

- [ ] **Step 5: Commit model abstraction**

~~~powershell
git add llm tests/test_model_adapter.py
git commit -m "feat: add LLM adapter and deterministic fallback"
~~~

### Task 7: Implement evidence pool, verification, and debate

**Files:**
- Create: evidence/__init__.py, evidence/pool.py, evidence/verifier.py, agents/debate.py, tests/test_evidence_and_debate.py

**Interfaces:**
- Consumes EvidenceRepository, Evidence, and ToolObservation.
- Produces EvidencePool.add/list/conflicts(), EvidenceVerifier.verify(), and DebateModerator.run().

- [ ] **Step 1: Write a failing conflict test**

~~~python
def test_conflicting_claims_are_marked_and_debate_cites_ids():
    pool = EvidencePool()
    pool.add(evidence("EV1", "公司融资", "已完成", "source-a"))
    pool.add(evidence("EV2", "公司融资", "未确认", "source-b"))
    assert {item.status for item in pool.list()} == {"conflicted"}
~~~

- [ ] **Step 2: Run the test to verify failure**

Run: pytest tests/test_evidence_and_debate.py::test_conflicting_claims_are_marked_and_debate_cites_ids -v

Expected: FAIL because EvidencePool is absent.

- [ ] **Step 3: Implement evidence dedupe, verification, and citation-only debate**

~~~python
def add(self, item: Evidence) -> Evidence:
    for existing in self._items.values():
        if existing.claim == item.claim and existing.value != item.value:
            existing.status = EvidenceStatus.CONFLICTED
            item.status = EvidenceStatus.CONFLICTED
    self._items[item.evidence_id] = item
    return item
~~~

Verification requests a second source through the Gateway only for important single-source or conflicted claims. Debate rejects an argument whose evidence ID is absent and returns agreements, disagreements, strongest pro/con, unresolved risks, and evidence quality.

- [ ] **Step 4: Run evidence and debate tests**

Run: pytest tests/test_evidence_and_debate.py -v

Expected: PASS for dedupe, conflict status, verification trigger, invalid citation rejection, and structured debate output.

- [ ] **Step 5: Commit evidence services**

~~~powershell
git add evidence agents/debate.py tests/test_evidence_and_debate.py
git commit -m "feat: add evidence verification and debate"
~~~

### Task 8: Implement planner, expert Agents, critic, and judge

**Files:**
- Create: agents/__init__.py, agents/base.py, agents/supervisor.py, agents/planner.py, agents/evidence_research.py, agents/financial_market.py, agents/location_lifestyle.py, agents/preference.py, agents/risk_critic.py, agents/judge.py, tests/test_agents.py

**Interfaces:**
- Consumes SkillDefinition, MemoryContext, MCPGateway, ModelAdapter, and EvidencePool.
- Produces Supervisor.classify(), Planner.create_plan(), BaseReActAgent.execute(), expert run(), RiskCritic.review(), and DecisionJudge.decide().

- [ ] **Step 1: Write a failing dynamic-routing and ReAct-limit test**

~~~python
async def test_cross_city_offer_routes_location_and_limits_react_to_three():
    plan = await planner.create_plan(offer_question("上海", "杭州"))
    assigned = {task.assigned_agent for task in plan.tasks}
    assert {"evidence_research", "preference", "location_lifestyle", "risk_critic"} <= assigned
    result = await looping_research_agent.execute(plan.tasks[0])
    assert result.tool_calls_used == 3
~~~

- [ ] **Step 2: Run the Agent test to verify failure**

Run: pytest tests/test_agents.py::test_cross_city_offer_routes_location_and_limits_react_to_three -v

Expected: FAIL because Planner and expert Agents are absent.

- [ ] **Step 3: Implement all roles with task-scoped context**

~~~python
class BaseReActAgent:
    max_tool_calls = 3

    async def execute(self, task: TaskSpec, context: AgentContext) -> AgentResult:
        for _ in range(self.max_tool_calls):
            action = await self.next_action(task, context)
            if action is None:
                return await self.finish(task, context)
            observation = await context.gateway.call(self.name, action.capability, action.arguments)
            context.observations.append(observation)
        return await self.finish(task, context)
~~~

Planner encodes required and conditional experts for every Skill. Preference reads only MemoryManager context. Research, finance, and location experts call only their mappings. Critic checks constraints and evidence quality. Judge rejects hard-constraint violations before weighted comparison and preserves every evidence category.

- [ ] **Step 4: Run Agent tests**

Run: pytest tests/test_agents.py -v

Expected: PASS for all domain routes, three-call cap, tool scopes, preference reads, critic arguments, and constraint rejection.

- [ ] **Step 5: Commit Agent layer**

~~~powershell
git add agents tests/test_agents.py
git commit -m "feat: add planning and expert agents"
~~~

### Task 9: Compose LangGraph workflow, replan, archive, and continuation

**Files:**
- Create: graph/__init__.py, graph/states.py, graph/decision_graph.py, graph/routing.py, tests/test_decision_graph.py

**Interfaces:**
- Consumes all Agents, MemoryManager, and TraceRepository.
- Produces DecisionGraph.run(), DecisionGraph.continue_decision(), and persisted DecisionState.

- [ ] **Step 1: Write a failing replan/continuation test**

~~~python
async def test_critical_tool_failure_replans_and_resume_uses_saved_state(tmp_path):
    graph = build_graph(tmp_path, gateway=unavailable_gateway())
    first = await graph.run(DecisionRequest(query="比较两款电脑", candidates=["A", "B"]))
    assert "REPLANNING" in [event.to_state for event in first.trace]
    resumed = await graph.continue_decision(first.decision_id)
    assert resumed.decision_id == first.decision_id
~~~

- [ ] **Step 2: Run the workflow test to verify failure**

Run: pytest tests/test_decision_graph.py::test_critical_tool_failure_replans_and_resume_uses_saved_state -v

Expected: FAIL because DecisionGraph is absent.

- [ ] **Step 3: Implement nodes and conditional routes**

~~~python
workflow = StateGraph(DecisionState)
workflow.add_node("plan", plan_node)
workflow.add_node("execute", execute_node)
workflow.add_conditional_edges(
    "execute",
    route_after_execution,
    {"replan": "plan", "verify": "verify", "debate": "debate", "judge": "judge"},
)
~~~

Persist a checkpoint and trace on every state transition. Verify conflicts and material single-source claims, debate material disagreements, archive only after a report, and permit another replan only when it reduces a critical unresolved gap.

- [ ] **Step 4: Run graph tests**

Run: pytest tests/test_decision_graph.py -v

Expected: PASS for normal archive, critical replan, verification, debate, and restoration from saved state.

- [ ] **Step 5: Commit orchestration graph**

~~~powershell
git add graph tests/test_decision_graph.py
git commit -m "feat: orchestrate decision workflow"
~~~

### Task 10: Expose the FastAPI application and feedback/retrospective flow

**Files:**
- Create: app/container.py, app/api/__init__.py, app/api/routes.py, app/main.py, main.py, tests/test_api.py

**Interfaces:**
- Consumes DecisionGraph, MemoryManager, SkillRegistry, and MCPGateway.
- Produces create_app(), ten required routes, and a PowerShell-compatible python main.py entrypoint.

- [ ] **Step 1: Write a failing API lifecycle test**

~~~python
async def test_decision_feedback_and_retrospective_lifecycle(client):
    created = await client.post("/decision", json={"query": "A or B", "candidates": ["A", "B"]})
    decision_id = created.json()["decision_id"]
    feedback = await client.post(f"/decision/{decision_id}/feedback", json={"user_choice": "A", "outcome": "满意"})
    assert feedback.status_code == 200
    retrospective = await client.post(f"/decision/{decision_id}/retrospective", json={})
    assert retrospective.status_code == 200
~~~

- [ ] **Step 2: Run the lifecycle test to verify failure**

Run: pytest tests/test_api.py::test_decision_feedback_and_retrospective_lifecycle -v

Expected: FAIL because create_app is absent.

- [ ] **Step 3: Implement dependency container, handlers, and retrospective**

~~~python
@router.post("/decision/{decision_id}/feedback")
async def feedback(
    decision_id: str,
    payload: FeedbackRequest,
    services: Services = Depends(get_services),
) -> FeedbackResponse:
    return await services.memory.record_feedback(decision_id, payload)
~~~

Retrospective loads archive and feedback, emits correct items, incorrect items, missing information, wrong assumptions, preference updates, and future lessons; it saves this result, writes an Episode, and runs reflection. Return 404 for unknown decision IDs and 422 for invalid payloads.

- [ ] **Step 4: Run API tests**

Run: pytest tests/test_api.py -v

Expected: PASS for decision, continue, reads, list, feedback, retrospective, memory, Skills, MCP tools, validation, and unknown IDs.

- [ ] **Step 5: Commit API layer**

~~~powershell
git add app main.py tests/test_api.py
git commit -m "feat: expose decision API"
~~~

### Task 11: Add acceptance tests, fixtures, and operational README

**Files:**
- Create: tests/conftest.py, tests/test_acceptance.py, README.md
- Modify: .gitignore

**Interfaces:**
- Consumes the completed API application and offline fake model/MCP fixtures.
- Produces reproducible five-scenario acceptance coverage and Chinese operational documentation.

- [ ] **Step 1: Write the required scenario tests**

~~~python
@pytest.mark.parametrize("query, expected_agents", [
    ("上海工作与杭州 AI Offer 怎么选", {"evidence_research", "location_lifestyle", "preference", "risk_critic"}),
    ("三款笔记本谁适合 8000 元预算", {"evidence_research", "preference", "risk_critic"}),
    ("杭州还是厦门周末旅行", {"evidence_research", "location_lifestyle", "preference", "risk_critic"}),
    ("我的 ETF 和股票是否过度集中", {"financial_market", "evidence_research", "preference", "risk_critic"}),
])
async def test_required_decision_scenarios(client, query, expected_agents):
    response = await client.post("/decision", json={"query": query})
    assert expected_agents <= set(response.json()["activated_agents"])
~~~

Create a separate test that creates a job Offer archive, saves feedback, and calls retrospective.

- [ ] **Step 2: Run acceptance tests to identify incomplete required behavior**

Run: pytest tests/test_acceptance.py -v

Expected: each scenario passes only after domain Skills, Agent routing, archive, and retrospective behavior are complete.

- [ ] **Step 3: Close test gaps and write README**

~~~markdown
## 离线降级

模型或 MCP 不可用时，系统仅使用用户输入、已保存偏好和已有 Evidence。它不将假设写成实时事实，报告会降低置信度并将缺失资料列入“未验证信息”。
~~~

Document features, directory map, reading order, complete request flow, multi-Agent, Plan-and-Execute/ReAct boundary, three memory levels, MCP registration/call flow, Skill authoring, autonomous decisions/safety harness, SQLite every table/every column, Qdrant collection/payload/vector, and all PowerShell configuration and startup commands for app and supported MCP services without Docker.

- [ ] **Step 4: Run full verification and a startup check**

Run: pytest -q

Expected: PASS for unit, API, graph, and acceptance tests.

Run: python main.py --check

Expected: prints initialized database path, discovered Skills, and configured MCP capability status without starting the HTTP server.

- [ ] **Step 5: Commit documentation and verified suite**

~~~powershell
git add README.md .gitignore tests
git commit -m "docs: document and verify decision agent"
~~~

## Plan self-review

| Specification requirement | Implementation task |
|---|---|
| Typed safe contracts and configuration | Task 1 |
| SQLite authority, tables, trace, and archives | Task 2 |
| Embedded Qdrant, search fallback, and reflection | Task 3 |
| Eight functional Skills | Task 4 |
| Stdio MCP discovery, mapping, policy, audit, fallback | Task 5 |
| OpenAI-compatible model and deterministic fallback | Task 6 |
| Evidence Pool, verification, critic, and debate | Tasks 7–8 |
| Dynamic Agents, Plan-and-Execute, and ReAct | Tasks 8–9 |
| State graph, replanning, and continuation | Task 9 |
| Required HTTP API and retrospective | Task 10 |
| Five acceptance scenarios, README, startup/configuration | Task 11 |

Every specified component is assigned to a task. Type and method names are introduced before consuming tasks. The plan has no deferred functionality.
