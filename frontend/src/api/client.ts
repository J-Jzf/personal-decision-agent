import type { DecisionApiResponse, DecisionRequest, DecisionReport, HITLResponse, WorkflowTraceEvent } from '../types'

export class DecisionApiError extends Error {
  constructor(message: string, public readonly status?: number) {
    super(message)
    this.name = 'DecisionApiError'
  }
}

function endpoint(): string {
  const environment = (import.meta as ImportMeta & { env?: { VITE_API_BASE_URL?: string } }).env
  const baseUrl = environment?.VITE_API_BASE_URL?.replace(/\/$/, '')
  return baseUrl ? `${baseUrl}/api/decision` : '/api/decision'
}

function streamEndpoint(): string {
  /** 返回与常规决策 API 同源的 SSE 流式端点。 */
  return `${endpoint()}/stream`
}

async function errorMessage(response: Response): Promise<string> {
  try {
    const body: unknown = await response.json()
    if (typeof body === 'object' && body !== null && 'detail' in body && typeof body.detail === 'string') {
      return body.detail
    }
  } catch {
    // A non-JSON error response still receives a useful normalized message.
  }

  return `决策服务请求失败（${response.status}）。`
}

export async function submitDecision(query: string): Promise<DecisionApiResponse> {
  const request: DecisionRequest = { query }

  let response: Response
  try {
    response = await fetch(endpoint(), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(request),
    })
  } catch {
    throw new DecisionApiError('无法连接决策服务，请稍后重试。')
  }

  if (!response.ok) {
    throw new DecisionApiError(await errorMessage(response), response.status)
  }

  try {
    return await response.json() as DecisionApiResponse
  } catch {
    throw new DecisionApiError('决策服务返回了无法识别的响应。', response.status)
  }
}

export type DecisionStreamHandlers = {
  onTrace: (event: WorkflowTraceEvent) => void
  onComplete: (response: DecisionApiResponse) => void
}

function parseSseBlock(block: string): { name: string; data: unknown } | null {
  /** 解析一帧 SSE，忽略注释、空帧和不完整的事件数据。 */
  let name = 'message'
  const data: string[] = []
  for (const line of block.replace(/\r/g, '').split('\n')) {
    if (line.startsWith('event:')) name = line.slice('event:'.length).trim()
    if (line.startsWith('data:')) data.push(line.slice('data:'.length).trimStart())
  }
  if (!data.length) return null
  try {
    return { name, data: JSON.parse(data.join('\n')) as unknown }
  } catch {
    throw new DecisionApiError('实时轨迹服务返回了无法识别的事件。')
  }
}

function isTraceEvent(value: unknown): value is WorkflowTraceEvent {
  /** 确保浏览器只把带稳定标识和类型的事件送入轨迹面板。 */
  return typeof value === 'object' && value !== null
    && typeof (value as { event_id?: unknown }).event_id === 'string'
    && typeof (value as { kind?: unknown }).kind === 'string'
}

export async function streamDecision(query: string, handlers: DecisionStreamHandlers): Promise<void> {
  /** 通过 POST + SSE 读取实时轨迹；网络断开会转换为前端可展示的错误。 */
  let response: Response
  try {
    response = await fetch(streamEndpoint(), {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query } satisfies DecisionRequest),
    })
  } catch {
    throw new DecisionApiError('无法连接实时决策服务，请稍后重试。')
  }
  if (!response.ok) throw new DecisionApiError(await errorMessage(response), response.status)
  if (!response.body) throw new DecisionApiError('浏览器不支持实时决策响应。', response.status)

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let completed = false

  const deliver = (block: string) => {
    /** 将一帧 SSE 分派给轨迹卡片或最终报告处理器。 */
    const message = parseSseBlock(block)
    if (!message) return
    if (message.name === 'decision_completed') {
      const responseData = (message.data as { response?: DecisionApiResponse }).response
      if (!responseData?.decision_id) throw new DecisionApiError('实时决策服务缺少最终结果。')
      handlers.onComplete(responseData)
      completed = true
      return
    }
    if (message.name === 'error') {
      const text = (message.data as { error?: unknown }).error
      throw new DecisionApiError(typeof text === 'string' ? text : '实时决策执行失败。')
    }
    if (isTraceEvent(message.data)) handlers.onTrace(message.data)
  }

  try {
    while (true) {
      const result = await reader.read()
      if (result.done) break
      buffer += decoder.decode(result.value, { stream: true })
      let boundary = buffer.indexOf('\n\n')
      while (boundary >= 0) {
        deliver(buffer.slice(0, boundary))
        buffer = buffer.slice(boundary + 2)
        boundary = buffer.indexOf('\n\n')
      }
    }
    buffer += decoder.decode()
    if (buffer.trim()) deliver(buffer)
  } catch (error) {
    if (error instanceof DecisionApiError) throw error
    throw new DecisionApiError('实时轨迹连接中断，决策仍可能在后台继续执行。')
  } finally {
    reader.releaseLock()
  }
  if (!completed) throw new DecisionApiError('实时轨迹连接已结束，但未收到最终决策结果。')
}

export async function respondToHitl(decisionId: string, requestId: string, response: HITLResponse): Promise<void> {
  const result = await fetch(`${endpoint()}/${decisionId}/hitl/${requestId}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(response) })
  if (!result.ok) throw new DecisionApiError(await errorMessage(result), result.status)
}

export async function deleteDecisionHistory(decisionIds: string[], deleteMemories: boolean): Promise<void> {
  if (!decisionIds.length) return
  const result = await fetch(`${endpoint().replace(/\/decision$/, '')}/decisions/delete`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ decision_ids: decisionIds, delete_memories: deleteMemories }) })
  if (!result.ok) throw new DecisionApiError(await errorMessage(result), result.status)
}

export async function submitFeedback(decisionId: string, userChoice: string, chosenReason: string, notChosenReason: string): Promise<void> {
  const result = await fetch(`${endpoint()}/${decisionId}/feedback`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ user_choice: userChoice, chosen_reason: chosenReason || null, not_chosen_reason: notChosenReason || null }) })
  if (!result.ok) throw new DecisionApiError(await errorMessage(result), result.status)
}

export async function requestRetrospective(decisionId: string): Promise<Record<string, unknown>> {
  const result = await fetch(`${endpoint()}/${decisionId}/retrospective`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })
  if (!result.ok) throw new DecisionApiError(await errorMessage(result), result.status)
  return await result.json() as Record<string, unknown>
}

function section(title: string, items: string[]): string {
  return [`## ${title}`, ...(items.length ? items.map((item) => `- ${item}`) : ['- 暂无'])].join('\n')
}

export function formatDecisionReport(response: Pick<DecisionApiResponse, 'report'>): string {
  const report: DecisionReport | null = response.report
  if (report === null) {
    return '## 决策结果\n- 决策服务未返回可用报告。'
  }

  const reasons = [
    ...report.confirmed_facts,
    ...report.external_views,
    ...report.inferences,
    ...report.preference_matches,
    ...report.tradeoffs,
  ]

  return [
    `## 推荐\n${report.recommended_option || '暂无'}`,
    `## 置信度\n${Math.round(report.confidence * 100)}%`,
    section('主要理由', reasons),
    section('风险', report.risks),
    section('不确定性', report.uncertainties),
  ].join('\n\n')
}
