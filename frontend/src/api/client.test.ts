import { afterEach, describe, expect, it, vi } from 'vitest'

import { DecisionApiError, formatDecisionReport, streamDecision, submitDecision } from './client'

const decision = {
  decision_id: 'decision-1',
  decision_type: 'general',
  status: 'completed',
  report: {
    recommended_option: '杭州',
    confidence: 0.8,
    confirmed_facts: ['通勤更短'],
    external_views: [],
    inferences: ['更适合当前目标'],
    preference_matches: [],
    uncertainties: ['岗位具体地点尚未确认'],
    rejected_options: [],
    tradeoffs: [],
    risks: ['生活成本较高'],
    next_verification_steps: [],
  },
  plan: null,
  events: [],
  activated_agents: [],
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('submitDecision', () => {
  it('posts only the query to the decision endpoint', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(decision), { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)

    await submitDecision('该选哪个')

    expect(fetchMock).toHaveBeenCalledWith('/api/decision', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query: '该选哪个' }),
    })
  })

  it('normalizes HTTP errors using the server detail', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: '服务暂不可用' }), { status: 503 })))

    await expect(submitDecision('该选哪个')).rejects.toEqual(
      expect.objectContaining<Partial<DecisionApiError>>({ status: 503, message: '服务暂不可用' }),
    )
  })

  it('normalizes network failures into a user-facing error', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')))

    await expect(submitDecision('该选哪个')).rejects.toEqual(
      expect.objectContaining<Partial<DecisionApiError>>({ message: '无法连接决策服务，请稍后重试。' }),
    )
  })
})

describe('streamDecision', () => {
  it('parses split SSE frames and delivers trace events before the completed response', async () => {
    const encoder = new TextEncoder()
    const trace = { event_id: 'trace-1', decision_id: 'decision-1', to_state: 'planned', kind: 'plan_created', title: '已制定计划', summary: '任务已分配', sequence: 1, payload: {}, created_at: '2026-08-18T00:00:00Z' }
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(encoder.encode(`event: plan_created\ndata: ${JSON.stringify(trace)}\n\n`))
        controller.enqueue(encoder.encode(`event: decision_completed\ndata: ${JSON.stringify({ decision_id: 'decision-1', response: decision })}\n\n`))
        controller.close()
      },
    })
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(stream, { status: 200 })))
    const events: string[] = []
    let completed = ''

    await streamDecision('上海还是贵州', {
      onTrace: (event) => events.push(event.kind),
      onComplete: (response) => { completed = response.decision_id },
    })

    expect(events).toEqual(['plan_created'])
    expect(completed).toBe('decision-1')
  })
})

describe('formatDecisionReport', () => {
  it('formats recommendation, confidence, reasons, risks and uncertainties', () => {
    expect(formatDecisionReport(decision)).toContain('## 推荐\n杭州')
    expect(formatDecisionReport(decision)).toContain('## 置信度\n80%')
    expect(formatDecisionReport(decision)).toContain('## 主要理由\n- 通勤更短')
    expect(formatDecisionReport(decision)).toContain('## 风险\n- 生活成本较高')
    expect(formatDecisionReport(decision)).toContain('## 不确定性\n- 岗位具体地点尚未确认')
  })
})
