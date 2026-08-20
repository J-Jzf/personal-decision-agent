import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import App from './App'

const decision = {
  decision_id: 'decision-1',
  decision_type: 'general',
  status: 'completed',
  report: {
    recommended_option: '杭州', confidence: 0.8, confirmed_facts: ['通勤更短'], external_views: [],
    inferences: [], preference_matches: [], uncertainties: ['岗位地点待确认'], rejected_options: [],
    tradeoffs: [], risks: ['生活成本较高'], next_verification_steps: [],
  },
  plan: null, events: [], activated_agents: [],
}

function streamResponse() {
  const trace = {
    event_id: 'trace-1', decision_id: 'decision-1', to_state: 'planned', kind: 'plan_created',
    title: '已制定执行计划', summary: '已分配旅行专家。', sequence: 1, payload: {}, created_at: '2026-08-18T00:00:00Z',
  }
  return new Response(
    `event: decision_started\ndata: ${JSON.stringify({ ...trace, event_id: 'trace-0', kind: 'decision_started', sequence: 0, title: '开始分析决策' })}\n\n`
    + `event: plan_created\ndata: ${JSON.stringify(trace)}\n\n`
    + `event: decision_completed\ndata: ${JSON.stringify({ decision_id: 'decision-1', response: { ...decision, events: [trace] } })}\n\n`,
    { status: 200 },
  )
}

afterEach(() => {
  cleanup()
  localStorage.clear()
  vi.unstubAllGlobals()
})

describe('App', () => {
  it('renders a new conversation with a right user message and left assistant result', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(streamResponse()))
    render(<App />)

    fireEvent.change(screen.getByRole('textbox'), { target: { value: '上海还是杭州' } })
    fireEvent.click(screen.getByRole('button', { name: '发送' }))

    expect(screen.getAllByText('上海还是杭州').some((element) => element.closest('[data-role="user"]'))).toBe(true)
    expect(await screen.findByText('杭州')).toBeTruthy()
    expect(screen.getByText('杭州').closest('[data-role="assistant"]')).toBeTruthy()
    expect(screen.getByText('实时决策轨迹')).toBeTruthy()
  })

  it('creates, selects, and restores conversation history', async () => {
    render(<App />)

    fireEvent.click(screen.getByRole('button', { name: /新对话/ }))
    fireEvent.change(screen.getByRole('textbox'), { target: { value: '第一条决定' } })
    fireEvent.click(screen.getByRole('button', { name: '发送' }))

    fireEvent.click(screen.getByRole('button', { name: /新对话/ }))
    expect(screen.getByRole('button', { name: '第一条决定' })).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: '第一条决定' }))
    expect(screen.getAllByText('第一条决定').some((element) => element.closest('[data-role="user"]'))).toBe(true)
  })

  it('shows an error with a retry action after a failed request', async () => {
    const fetchMock = vi.fn()
      .mockRejectedValueOnce(new TypeError('offline'))
      .mockResolvedValueOnce(streamResponse())
    vi.stubGlobal('fetch', fetchMock)
    render(<App />)

    fireEvent.change(screen.getByRole('textbox'), { target: { value: '重试测试' } })
    fireEvent.click(screen.getByRole('button', { name: '发送' }))
    expect(await screen.findByRole('button', { name: '重试' })).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: '重试' }))

    expect(await screen.findByText('杭州')).toBeTruthy()
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })
})
