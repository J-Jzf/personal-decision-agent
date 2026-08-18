# Personal Decision Agent — 详细设计

## 1. 目标与边界

Personal Decision Agent 是一个本地运行的 FastAPI 后端，用统一的复杂决策框架处理工作 Offer、产品比较、旅行/地点、课程/订阅、投资组合与通用利弊决策。系统提供多 Agent 编排、Plan-and-Execute、专家内部 ReAct、结构化 Evidence Pool、三级个人记忆、决策档案、复盘和本地 MCP 工具接入。

系统只做信息获取、分析、比较和建议；永不执行交易、购买、预订、接受 Offer、Shell 命令、任意代码或本地文件写入。没有前端。

## 2. 运行时架构

入口 `main.py` 启动 FastAPI。`app` 层负责配置、依赖装配和 API；`graph` 层使用 LangGraph 持有可恢复的 `DecisionState`；`agents` 层仅处理结构化任务；`memory` 是唯一能操作存储的访问层；`mcp` 将外部标准 MCP 服务映射为受控内部能力。

决策状态严格按以下状态机推进：

```text
RECEIVED -> CLASSIFIED -> MEMORY_RETRIEVED -> SKILL_LOADED -> PLANNED
-> EXECUTING <-> REPLANNING -> VERIFYING -> DEBATING -> JUDGING
-> COMPLETED -> ARCHIVED
```

DecisionManager 分类复杂度和领域、加载 Skill、读取记忆并驱动图。复杂任务由 Planner 形成带依赖和完成条件的计划。Executor 根据依赖和任务 Agent 调度工作；重规划只在关键资料缺失、证据冲突、硬约束违规、Critic 提出关键遗漏或工具持续不可用时发生。

## 3. Agent 与决策范式

编排角色包括 DecisionManager/Supervisor、Planner 与 DecisionJudge。专家角色包括 EvidenceResearch、FinancialMarket、LocationLifestyle、Preference、RiskCritic。分类规则决定最小专家集合，并按搬家、通勤、RSU/股票、金融资产等条件增加专家。

Planner 使用 Plan-and-Execute：输出 Pydantic 约束的目标、候选、约束、偏好、缺失信息、任务 DAG、能力、Agent、完成条件、辩论标志、验证标志和重规划条件。专家之间只交换 `Task`、`AgentResult`、必要记忆和 Evidence ID，不交换完整中间推理。

可调用工具的专家以最多三轮 ReAct 运行：先评估缺口，选择一项白名单能力，经 Gateway 调用，读取 Observation，再决定继续或产生结果。无可用工具、空结果、调用错误与超过轮次均生成可追溯的不确定性，而非杜撰事实。

RiskCritic 对证据质量、遗漏、反例、陈旧性、硬约束与过度推断做对抗检查。仅当争议显著、重要证据冲突或 Skill 强制要求时运行辩论；Moderator 只接受带 Evidence ID 的正反论据，并输出共识、分歧、最强双方论点、未解决风险和证据质量。

DecisionJudge 区分确认事实、外部观点、Agent 推断、个人历史偏好和未验证信息，先淘汰硬约束违规选项，再按偏好与证据生成排序、推荐、置信度、取舍、风险、不确定性和下一步核实项。

## 4. LLM 与离线降级

`ModelAdapter` 从 `.env` 的 `LLM_MODEL_ID`、`LLM_API_KEY`、`LLM_BASE_URL` 调用 OpenAI 兼容聊天接口，并要求结构化 JSON 输出。各角色可配置为同一模型。

模型缺失、超时、响应无效或服务错误时切换到 `DeterministicReasoner`：它解析候选和显式硬约束、根据 Skill 维度与 Profile 权重评分、使用已保存的历史和证据、形成可解释报告。它不产生任何本应来自实时检索的事实；缺失字段写入 `uncertainties`，置信度上限下调。模型恢复后新请求自动重新采用模型。所有降级事件写入工作状态和 trace。

## 5. 存储模型

SQLite 位于 `var/personal_decision.db`，是所有结构化事实和事务的源头。包含：

| 表 | 列 | 用途 |
|---|---|---|
| `decision_archives` | `decision_id`, `decision_type`, `query`, `status`, `candidates_json`, `constraints_json`, `preferences_json`, `plan_json`, `report_json`, `recommendation`, `confidence`, `created_at`, `updated_at` | 完整决策档案 |
| `working_memories` | `decision_id`, `state_json`, `checkpoint_version`, `updated_at` | 可恢复图状态 |
| `episodes` | `episode_id`, `decision_id`, `decision_type`, `summary`, `options_json`, `recommendation`, `user_choice`, `key_reasons_json`, `tradeoffs_json`, `outcome`, `feedback`, `tags_json`, `profile_signals_json`, `created_at` | 精炼历史事件 |
| `profile_memories` | `id`, `category`, `memory_key`, `value_json`, `importance`, `confidence`, `source_episode_ids_json`, `created_at`, `updated_at` | 稳定偏好与证据来源 |
| `evidence` | `evidence_id`, `decision_id`, `claim`, `value_json`, `source`, `source_type`, `agent`, `tool`, `confidence`, `freshness`, `status`, `supports_json`, `contradicts_json`, `retrieved_at` | Evidence Pool |
| `agent_results` | `id`, `decision_id`, `task_id`, `agent_name`, `result_json`, `completion_status`, `created_at` | Agent 结构化交接 |
| `tool_calls` | `call_id`, `decision_id`, `task_id`, `agent_name`, `tool_name`, `arguments_json`, `status`, `latency_ms`, `result_summary`, `error`, `created_at` | 工具审计 |
| `workflow_events` | `id`, `decision_id`, `from_state`, `to_state`, `payload_json`, `created_at` | 状态 trace |
| `feedback` | `id`, `decision_id`, `user_choice`, `outcome`, `notes`, `created_at` | 用户实际选择与结果 |
| `retrospectives` | `id`, `decision_id`, `result_json`, `created_at` | 复盘输出 |

嵌入式 Qdrant 位于 `var/qdrant`，使用 `QdrantClient(path="var/qdrant")`，只有 `episodes` collection。每个 point 的 id 是 `episode_id`；payload 有 `decision_id`、`decision_type`、`tags`、`created_at`；向量为本地哈希嵌入。Qdrant 仅加速元数据过滤后的 episode 语义近邻，SQLite 始终保存原始内容并可在 Qdrant 缺失或故障时提供关键词/标签/时间回退检索。

`MemoryManager` 是唯一访问 SQLite/Qdrant 的入口。Working Memory 由 LangGraph state 与 SQLite checkpoint 持久化；完成时依次写 Archive、抽取 Episode、更新 Qdrant、运行 Profile Reflection。明确长期偏好可直接入库；单次推断仅记 Episode；多个独立 Episode 一致才强化；冲突降低置信度，明确改变偏好才替换值。

## 6. MCP 工具层

`MCPGateway` 使用本地 stdio 客户端管理服务器会话。启动时根据配置命令连接服务器、自动发现工具 schema、写入 ToolRegistry；内部能力 `web_search`、`fetch_page`、`place_search`、`route_search`、`weather_forecast`、`market_data` 映射到已发现的实际工具名，允许同能力多个提供方。

每次调用依次检查能力存在、Agent 白名单、只读动作名称/参数、单任务次数上限和超时。调用后 Adapter 将不同 MCP 返回统一为 `ToolObservation`，并将参数、状态、摘要、错误、延迟写入 `tool_calls`。工具失败时选择同能力替代工具；没有替代时返回 `unavailable` Observation 给专家，并在最终报告标明不可验证项。

允许动作是搜索、获取、查询、读取、列举、查看和比较。Gateway 拒绝代码执行、Shell、安装、删除、本地文件写入、资金转移、下单、买卖、预订、购买、提交和接受 Offer 等动作，即使外部服务器声明此类工具。

## 7. Skills 与 API

SkillRegistry 扫描八个 `skills/<name>/SKILL.md`，解析 YAML front matter，并验证名称、触发条件、输入、推荐 Agent、推荐能力、分析维度、工作流、风险检查、完成条件、输出 schema。领域 Skill 驱动 Planner 的任务和维度；`evidence_verification` 与 `risk_debate_moderator` 可由风险规则叠加。实现的 Skills：`job_offer_evaluator`、`product_comparison`、`travel_destination_compare`、`portfolio_review`、`course_subscription_evaluator`、`risk_debate_moderator`、`evidence_verification`、`decision_retrospective`。

FastAPI 提供 `POST /decision`、`POST /decision/{id}/continue`、`GET /decision/{id}`、`GET /decisions`、`POST /decision/{id}/feedback`、`POST /decision/{id}/retrospective`、`GET /memory/profile`、`GET /memory/episodes`、`GET /skills`、`GET /mcp/tools`。写入型 HTTP 端点只写本地数据库；所有现实世界行动型 MCP 能力都不可达。

## 8. 质量、可观测性与验收

每个状态转换、任务结果、重规划、模型降级和工具调用产生持久化 trace。`POST /decision` 返回报告和 trace；继续接口从 SQLite 状态恢复。

pytest 使用本地 fake model 和 fake MCP session，不依赖网络，验证五个需求验收场景、动态专家选择、硬约束淘汰、三轮 ReAct、权限阻断、失败降级、二次验证、重规划、辩论、SQLite/Qdrant 检索、记忆反思、反馈/复盘和全部 API。真实 MCP 仅在使用者按 README 配置并启动本地服务时启用。

## 9. 项目目录

```text
app/        API、设置、依赖组装
agents/     编排和五类专家
graph/      LangGraph 状态、路由、执行与重规划
memory/     SQLite/Qdrant 仓储、检索、提取与反思
mcp/        注册表、Gateway、客户端、适配器和策略
evidence/   证据池和验证器
models/     Pydantic 通信与持久化模型
skills/     8 个可加载 SOP
tests/      单元、集成与验收测试
var/        运行时 SQLite 和嵌入式 Qdrant 数据（忽略版本控制）
```

## 10. 不变量

1. Agent 不直接访问数据库、第三方 SDK 或 MCP session。
2. 外部事实必须来自 Evidence Pool；离线策略不能生成伪造事实。
3. 工具默认拒绝，只有显式注册、只读并获授权时才可调用。
4. 所有跨层交互使用 Pydantic 模型；无未验证的自由文本契约。
5. SQLite 是持久化权威来源，Qdrant 是可重建索引。
6. 报告始终显式标注事实、观点、推断、偏好和不确定性。
