export type ChatMessage = {
  id: string
  role: 'user' | 'assistant' | 'error' | 'trace'
  content: string
  createdAt: string
  trace?: TraceMessageState
}

/** 后端持久化并通过 SSE 逐步发送的一条可审计执行事件。 */
export type WorkflowTraceEvent = {
  event_id: string
  decision_id: string
  from_state?: string | null
  to_state: string
  kind: string
  title: string
  summary: string
  sequence: number
  payload: Record<string, unknown>
  created_at: string
}

/** 一张前端轨迹卡片在进行中、已完成或异常时的展示状态。 */
export type TraceMessageState = {
  status: 'running' | 'completed' | 'error' | 'disconnected'
  events: WorkflowTraceEvent[]
  decisionId?: string
  error?: string
}

export type ChatConversation = {
  id: string
  title: string
  createdAt: string
  updatedAt: string
  decisionId?: string
  candidates?: string[]
  messages: ChatMessage[]
}

export type HITLResponse = { values?: Record<string, string>; free_text?: string; skip?: boolean }

export type DecisionRequest = {
  query: string
}

export type DecisionReport = {
  recommended_option: string
  confidence: number
  confirmed_facts: string[]
  external_views: string[]
  inferences: string[]
  preference_matches: string[]
  uncertainties: string[]
  rejected_options: string[]
  tradeoffs: string[]
  risks: string[]
  next_verification_steps: string[]
}

export type DecisionApiResponse = {
  decision_id: string
  decision_type: string
  status: string
  report: DecisionReport | null
  plan: unknown | null
  events: WorkflowTraceEvent[]
  activated_agents: string[]
  candidates: string[]
}

export function createConversation(title: string): ChatConversation {
  const timestamp = new Date().toISOString()

  return {
    id: crypto.randomUUID(),
    title,
    createdAt: timestamp,
    updatedAt: timestamp,
    messages: [],
  }
}
