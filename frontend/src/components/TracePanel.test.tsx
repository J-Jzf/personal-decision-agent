import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { TracePanel } from './TracePanel'


describe('TracePanel', () => {
  it('shows plan, expert, tool observation and retry details in Chinese', () => {
    render(<TracePanel status="running" events={[
      { event_id: '1', decision_id: 'd-1', to_state: 'planned', kind: 'plan_created', title: '已制定执行计划', summary: '已分配专家任务。', sequence: 1, payload: { plan: { tasks: [{ task_id: 'travel', agent: 'location_lifestyle' }] } }, created_at: '2026-08-18T00:00:00Z' },
      { event_id: '2', decision_id: 'd-1', to_state: 'executing', kind: 'tool_observation', title: '工具观察结果', summary: '上海周末天气晴。', sequence: 2, payload: { tool: 'weather_forecast', status: 'succeeded' }, created_at: '2026-08-18T00:00:01Z' },
      { event_id: '3', decision_id: 'd-1', to_state: 'executing', kind: 'tool_retry', title: '工具调用超时，正在重试', summary: '首次调用未完成。', sequence: 3, payload: { reason: 'timed out' }, created_at: '2026-08-18T00:00:02Z' },
    ]} />)

    expect(screen.getByText('实时决策轨迹')).toBeTruthy()
    expect(screen.getByText('任务计划')).toBeTruthy()
    expect(screen.getByText('travel')).toBeTruthy()
    expect(screen.getByLabelText('任务 travel：未完成')).toBeTruthy()
    expect(screen.getByText('已制定执行计划')).toBeTruthy()
    expect(screen.getByText(/天气晴/)).toBeTruthy()
    expect(screen.getByText(/正在重试/)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: /查看第 1 步详情/ }))
    expect(screen.getAllByText(/location_lifestyle/).length).toBeGreaterThan(0)
  })

  it('marks a task as completed when its expert task completion event arrives', () => {
    render(<TracePanel status="running" events={[
      { event_id: '1', decision_id: 'd-1', to_state: 'planned', kind: 'plan_created', title: '已制定执行计划', summary: '', sequence: 1, payload: { plan: { tasks: [{ task_id: 'weather', objective: '查询天气', agent: 'location_lifestyle' }] } }, created_at: '2026-08-18T00:00:00Z' },
      { event_id: '2', decision_id: 'd-1', to_state: 'executing', kind: 'agent_task_completed', title: '专家 Agent 完成任务', summary: '', sequence: 2, payload: { task_id: 'weather', completion_status: 'completed' }, created_at: '2026-08-18T00:00:01Z' },
    ]} />)

    expect(screen.getByLabelText('任务 weather：已完成')).toBeTruthy()
    expect(screen.getByText('查询天气')).toBeTruthy()
  })

  it('merges a resolved information target back into the expert target plan', () => {
    render(<TracePanel status="running" events={[
      { event_id: '1', decision_id: 'd-1', to_state: 'executing', kind: 'expert_information_plan', title: '专家信息目标计划', summary: '', sequence: 1, payload: { task_id: 'weather', targets: [{ target_id: 'comparison', objective: '比较两地天气', status: 'pending', tool_calls_used: 0 }] }, created_at: '2026-08-18T00:00:00Z' },
      { event_id: '2', decision_id: 'd-1', to_state: 'executing', kind: 'information_target_resolved', title: '专家已结算信息目标', summary: '', sequence: 2, payload: { task_id: 'weather', target_id: 'comparison', resolution: { status: 'complete' } }, created_at: '2026-08-18T00:00:01Z' },
    ]} />)

    expect(screen.getByText('complete · 0/3')).toBeTruthy()
  })

  it('renders a partial target as a half-filled status circle', () => {
    const { container } = render(<TracePanel status="running" events={[
      { event_id: '1', decision_id: 'd-1', to_state: 'executing', kind: 'expert_information_plan', title: '专家信息目标计划', summary: '', sequence: 1, payload: { task_id: 'weather', targets: [{ target_id: 'comparison', objective: '比较两地天气', status: 'pending', tool_calls_used: 0 }] }, created_at: '2026-08-18T00:00:00Z' },
      { event_id: '2', decision_id: 'd-1', to_state: 'executing', kind: 'information_coverage_updated', title: '已更新当前信息目标覆盖状态', summary: '', sequence: 2, payload: { task_id: 'weather', updates: [{ target_key: 'comparison', status: 'partial' }] }, created_at: '2026-08-18T00:00:01Z' },
    ]} />)

    expect(container.querySelector('.task-dot-partial')).toBeTruthy()
  })
})
