import { useEffect, useMemo, useState } from 'react'

import type { TraceMessageState, WorkflowTraceEvent } from '../types'
import { HITLPrompt } from './HITLPrompt'

type TracePanelProps = Pick<TraceMessageState, 'events' | 'status' | 'error'> & { onHitlResponse?: (decisionId: string, requestId: string, values: Record<string, string>, freeText: string, skip: boolean) => void }

type PlanTask = { task_id: string; objective?: string; agent?: string; completed: boolean }
type InformationTarget = { key: string; taskId: string; targetId: string; objective: string; status: string; calls: number }

const statusLabel: Record<TraceMessageState['status'], string> = {
  running: '正在执行',
  completed: '执行完成',
  error: '执行失败',
  disconnected: '实时连接中断',
}

function safeDetail(value: unknown, key = ''): unknown {
  /** 在浏览器端再次遮蔽敏感字段，防御旧轨迹或异常后端返回。 */
  if (/^(?:[a-z0-9]+_)?(?:api_?key|access_?token|refresh_?token|id_?token|token|secret|client_?secret|authorization|password)$/i.test(key)) return '***'
  if (typeof value === 'string') return value.length > 1500 ? `${value.slice(0, 1500)}…（已截断）` : value
  if (Array.isArray(value)) return value.map((item) => safeDetail(item, key))
  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.entries(value as Record<string, unknown>).map(([itemKey, itemValue]) => [itemKey, safeDetail(itemValue, itemKey)]))
  }
  return value
}

function eventIcon(kind: string): string {
  /** 为不同的结构化轨迹事件提供快速可辨识的视觉标记。 */
  if (kind.includes('plan')) return '计划'
  if (kind.includes('agent')) return '专家'
  if (kind.includes('tool') || kind.includes('react')) return '工具'
  if (kind.includes('verification')) return '核验'
  if (kind.includes('failed')) return '异常'
  return '流程'
}

function planTasks(events: WorkflowTraceEvent[]): PlanTask[] {
  /** 从计划与专家完成事件重建当前决策的任务进度，重规划时保留既有完成项。 */
  const tasks = new Map<string, PlanTask>()
  for (const event of events) {
    const payload = event.payload
    if ((event.kind === 'plan_created' || event.kind === 'plan_replanned') && payload.plan && typeof payload.plan === 'object') {
      const rawTasks = (payload.plan as { tasks?: unknown }).tasks
      if (Array.isArray(rawTasks)) {
        for (const rawTask of rawTasks) {
          if (!rawTask || typeof rawTask !== 'object') continue
          const task = rawTask as { task_id?: unknown; objective?: unknown; agent?: unknown; status?: unknown }
          if (typeof task.task_id !== 'string') continue
          const previous = tasks.get(task.task_id)
          tasks.set(task.task_id, {
            task_id: task.task_id,
            objective: typeof task.objective === 'string' ? task.objective : previous?.objective,
            agent: typeof task.agent === 'string' ? task.agent : previous?.agent,
            completed: previous?.completed ?? task.status === 'completed',
          })
        }
      }
    }
    if (event.kind === 'agent_task_completed' && typeof payload.task_id === 'string') {
      const task = tasks.get(payload.task_id)
      if (task) task.completed = payload.completion_status === 'completed'
    }
    if (event.kind === 'replan_reused_work' && Array.isArray(payload.reused_completed_task_ids)) {
      for (const taskId of payload.reused_completed_task_ids) {
        if (typeof taskId === 'string' && tasks.has(taskId)) tasks.get(taskId)!.completed = true
      }
    }
  }
  return [...tasks.values()]
}

function informationTargets(events: WorkflowTraceEvent[]): InformationTarget[] {
  /** 从专家内部计划和覆盖更新事件重建每项信息目标的可见状态。 */
  const targets = new Map<string, InformationTarget>()
  for (const event of events) {
    const payload = event.payload
    if (event.kind === 'expert_information_plan' && typeof payload.task_id === 'string' && Array.isArray(payload.targets)) {
      for (const raw of payload.targets) {
        if (!raw || typeof raw !== 'object') continue
        const item = raw as { target_id?: unknown; objective?: unknown; status?: unknown; tool_calls_used?: unknown }
        if (typeof item.target_id !== 'string') continue
        const key = `${payload.task_id}:${item.target_id}`
        targets.set(key, { key, taskId: payload.task_id, targetId: item.target_id, objective: typeof item.objective === 'string' ? item.objective : item.target_id, status: typeof item.status === 'string' ? item.status : 'pending', calls: typeof item.tool_calls_used === 'number' ? item.tool_calls_used : 0 })
      }
    }
    if (event.kind === 'information_coverage_updated' && typeof payload.task_id === 'string' && Array.isArray(payload.updates)) {
      for (const raw of payload.updates) {
        if (!raw || typeof raw !== 'object') continue
        const update = raw as { target_key?: unknown; status?: unknown }
        if (typeof update.target_key !== 'string') continue
        const target = targets.get(`${payload.task_id}:${update.target_key}`)
        if (target && typeof update.status === 'string') target.status = update.status
      }
    }
    if (event.kind === 'information_target_resolved' && typeof payload.task_id === 'string' && typeof payload.target_id === 'string') {
      // 目标通过 finish 结算时不会再产生覆盖更新；此处将结算事件合并回顶部计划卡片。
      const target = targets.get(`${payload.task_id}:${payload.target_id}`)
      const resolution = payload.resolution
      if (target && resolution && typeof resolution === 'object') {
        const status = (resolution as { status?: unknown }).status
        if (typeof status === 'string') target.status = status
      }
    }
    if (event.kind === 'information_target_status' && typeof payload.task_id === 'string' && typeof payload.target_id === 'string') {
      const target = targets.get(`${payload.task_id}:${payload.target_id}`)
      if (target) {
        if (typeof payload.status === 'string') target.status = payload.status
        if (typeof payload.tool_calls_used === 'number') target.calls = payload.tool_calls_used
      }
    }
    if (event.kind === 'tool_observation' && typeof payload.task_id === 'string' && typeof payload.target_id === 'string') {
      const target = targets.get(`${payload.task_id}:${payload.target_id}`)
      if (target) target.calls += 1
    }
  }
  return [...targets.values()]
}

export function TracePanel({ events, status, error, onHitlResponse }: TracePanelProps) {
  /** 渲染可折叠的、按 SSE 到达顺序增长的可审计决策轨迹。 */
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const latestId = events.length ? events[events.length - 1].event_id : undefined

  useEffect(() => {
    /** 默认只展开最新一步，避免较长任务的轨迹淹没聊天页面。 */
    if (latestId) setExpanded(new Set([latestId]))
  }, [latestId])

  const orderedEvents = useMemo(() => [...events].sort((left, right) => left.sequence - right.sequence), [events])
  const tasks = useMemo(() => planTasks(orderedEvents), [orderedEvents])
  const targets = useMemo(() => informationTargets(orderedEvents), [orderedEvents])

  return (
    <section aria-label="实时决策轨迹" className={`trace-panel trace-${status}`}>
      <header className="trace-header">
        <div><strong>实时决策轨迹</strong><span>{statusLabel[status]}</span></div>
        <span>{orderedEvents.length} 步</span>
      </header>
      {tasks.length > 0 && <section aria-label="任务计划" className="trace-plan">
        <strong>任务计划</strong>
        <ol>
          {tasks.map((task) => <li key={task.task_id}>
            <span aria-label={`任务 ${task.task_id}：${task.completed ? '已完成' : '未完成'}`} className={`task-dot ${task.completed ? 'task-dot-completed' : ''}`} />
            <span className="task-plan-copy">{task.objective || task.task_id}</span>
            {task.agent && <small>{task.agent}</small>}
          </li>)}
        </ol>
      </section>}
      {targets.length > 0 && <section aria-label="专家信息目标计划" className="trace-plan trace-target-plan">
        <strong>专家信息目标计划</strong>
        <ol>{targets.map((target) => <li key={target.key}>
          <span className={`task-dot ${target.status === 'complete' ? 'task-dot-completed' : target.status === 'partial' ? 'task-dot-partial' : ''}`} />
          <span className="task-plan-copy">{target.objective}</span><small>{target.status} · {target.calls}/3</small>
        </li>)}</ol>
      </section>}
      <p className="trace-note">展示计划、专家分工、工具观察与异常处理；不展示模型私有思维链。</p>
      <ol className="trace-events">
        {orderedEvents.map((event, index) => {
          const isExpanded = expanded.has(event.event_id)
          const detail = safeDetail(event.payload)
          return <li className={`trace-event trace-kind-${event.kind}`} key={event.event_id}>
            <button
              aria-expanded={isExpanded}
              aria-label={`查看第 ${index + 1} 步详情：${event.title}`}
              className="trace-event-summary"
              onClick={() => setExpanded((current) => {
                const next = new Set(current)
                if (next.has(event.event_id)) next.delete(event.event_id)
                else next.add(event.event_id)
                return next
              })}
              type="button"
            >
              <span className="trace-sequence">{event.sequence || index + 1}</span>
              <span className="trace-icon">{eventIcon(event.kind)}</span>
              <span className="trace-copy"><strong>{event.title}</strong><span>{event.summary || '已完成该执行步骤。'}</span></span>
              <span aria-hidden="true" className="trace-toggle">{isExpanded ? '收起' : '详情'}</span>
            </button>
            {isExpanded && <pre className="trace-detail">{JSON.stringify(detail, null, 2)}</pre>}
            {event.kind === 'hitl_requested' && onHitlResponse && typeof event.payload.request === 'object' && event.payload.request !== null && (() => {
              const request = event.payload.request as { request_id?: string; question?: string; fields?: Array<{ key: string; label: string; placeholder?: string; required?: boolean }> }
              return request.request_id && request.question ? <HITLPrompt request={{ request_id: request.request_id, question: request.question, fields: request.fields }} timeoutSeconds={typeof event.payload.timeout_seconds === 'number' ? event.payload.timeout_seconds : 20} onSubmit={(values, freeText, skip) => onHitlResponse(event.decision_id, request.request_id!, values, freeText, skip)} /> : null
            })()}
          </li>
        })}
      </ol>
      {error && <p className="trace-error">{error}</p>}
    </section>
  )
}

export type { TracePanelProps, WorkflowTraceEvent }
