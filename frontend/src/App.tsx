import { useCallback, useState } from 'react'

import { deleteDecisionHistory, formatDecisionReport, requestRetrospective, respondToHitl, streamDecision, submitFeedback } from './api/client'
import { DeleteConversationDialog } from './components/DeleteConversationDialog'
import { FeedbackDialog } from './components/FeedbackDialog'
import { SkillCapabilities } from './components/SkillCapabilities'
import { Composer } from './components/Composer'
import { MessageBubble } from './components/MessageBubble'
import { Sidebar } from './components/Sidebar'
import { TracePanel } from './components/TracePanel'
import { useConversations } from './hooks/useConversations'
import type { WorkflowTraceEvent } from './types'

export default function App() {
  const { conversations, activeConversation, activeConversationId, appendMessage, updateMessage, newConversation, selectConversation, setDecisionId, setCandidates, decisionIds, deleteConversation } = useConversations()
  const [isLoading, setIsLoading] = useState(false)
  const [retry, setRetry] = useState<{ conversationId: string; query: string } | null>(null)
  const [deletingConversationId, setDeletingConversationId] = useState<string | null>(null)
  const [feedbackOpen, setFeedbackOpen] = useState(false)
  const [feedbackSubmitted, setFeedbackSubmitted] = useState<Record<string, boolean>>({})

  const runDecision = useCallback(async (query: string, conversationId: string) => {
    setIsLoading(true)
    setRetry(null)
    const traceMessage = appendMessage(conversationId, {
      role: 'trace', content: '', trace: { status: 'running', events: [] },
    })
    try {
      await streamDecision(query, {
        onTrace: (event) => {
          if (event.decision_id) setDecisionId(conversationId, event.decision_id)
          updateMessage(conversationId, traceMessage.id, (message) => ({
            ...message,
            trace: {
              status: 'running', decisionId: event.decision_id,
              events: [...(message.trace?.events ?? []).filter((item) => item.event_id !== event.event_id), event]
                .sort((left, right) => left.sequence - right.sequence),
            },
          }))
        },
        onComplete: (decision) => {
          const knownEvents = new Map<string, WorkflowTraceEvent>()
          updateMessage(conversationId, traceMessage.id, (message) => {
            for (const event of [...(message.trace?.events ?? []), ...decision.events]) knownEvents.set(event.event_id, event)
            return {
              ...message,
              trace: { status: 'completed', decisionId: decision.decision_id,
                events: [...knownEvents.values()].sort((left, right) => left.sequence - right.sequence) },
            }
          })
          appendMessage(conversationId, { role: 'assistant', content: formatDecisionReport(decision) })
          setDecisionId(conversationId, decision.decision_id)
          setCandidates(conversationId, decision.candidates)
        },
      })
    } catch (error) {
      const content = error instanceof Error ? error.message : '决策服务暂时不可用，请稍后重试。'
      updateMessage(conversationId, traceMessage.id, (message) => ({
        ...message,
        trace: { ...(message.trace ?? { events: [] }), status: content.includes('连接中断') ? 'disconnected' : 'error', error: content },
      }))
      appendMessage(conversationId, { role: 'error', content })
      setRetry({ conversationId, query })
    } finally {
      setIsLoading(false)
    }
  }, [appendMessage, setDecisionId, updateMessage])

  const send = (query: string) => {
    const conversation = activeConversation ?? newConversation(query)
    appendMessage(conversation.id, { role: 'user', content: query })
    void runDecision(query, conversation.id)
  }

  const retryFailedDecision = () => {
    if (retry) void runDecision(retry.query, retry.conversationId)
  }

  const confirmDelete = async (deleteMemories: boolean) => {
    if (!deletingConversationId) return
    const ids = decisionIds(deletingConversationId)
    try { await deleteDecisionHistory(ids, deleteMemories) } catch { /* 未提交到后端的本地新对话仍可安全删除。 */ }
    deleteConversation(deletingConversationId); setDeletingConversationId(null)
  }

  const saveFeedback = async (choice: string, chosenReason: string, notChosenReason: string) => {
    if (!activeConversation?.decisionId) return
    await submitFeedback(activeConversation.decisionId, choice, chosenReason, notChosenReason)
    setFeedbackSubmitted((current) => ({ ...current, [activeConversation.id]: true })); setFeedbackOpen(false)
  }

  const startRetrospective = async () => {
    if (!activeConversation?.decisionId) return
    const result = await requestRetrospective(activeConversation.decisionId)
    appendMessage(activeConversation.id, { role: 'assistant', content: `## 决策复盘\n\n\`\`\`json\n${JSON.stringify(result, null, 2)}\n\`\`\`` })
  }

  return (
    <div className="app-shell">
      <Sidebar
        activeConversationId={activeConversationId}
        conversations={conversations}
        onNewConversation={() => newConversation()}
        onSelectConversation={selectConversation}
        onDeleteConversation={setDeletingConversationId}
      />
      <main className="chat">
        <header className="chat-header"><h1>Decision Agent</h1><span>把复杂选择讲清楚</span></header>
        <section aria-live="polite" className="messages">
          <SkillCapabilities />
          {!activeConversation && <div className="empty-state"><h2>开始一段新对话</h2><p>描述你的选择、限制和在意的事情。</p></div>}
          {activeConversation?.messages.map((message) => message.role === 'trace'
            ? <TracePanel key={message.id} error={message.trace?.error} events={message.trace?.events ?? []} status={message.trace?.status ?? 'completed'} onHitlResponse={(decisionId, requestId, values, freeText, skip) => { void respondToHitl(decisionId, requestId, { values, free_text: freeText, skip }) }} />
            : <MessageBubble key={message.id} message={message} onRetry={message.role === 'error' && retry?.conversationId === activeConversationId ? retryFailedDecision : undefined} />
          )}
          {isLoading && <div className="loading" role="status">正在整理决策建议…</div>}
        </section>
        <div className="composer-actions">
          {activeConversation?.decisionId && !feedbackSubmitted[activeConversation.id] && <button type="button" onClick={() => setFeedbackOpen(true)}>提交反馈</button>}
          {activeConversation?.decisionId && feedbackSubmitted[activeConversation.id] && <button type="button" onClick={() => { void startRetrospective() }}>开始复盘</button>}
        </div>
        <Composer disabled={isLoading} onSend={send} />
      </main>
      {deletingConversationId && <DeleteConversationDialog title={conversations.find((item) => item.id === deletingConversationId)?.title ?? '此对话'} onCancel={() => setDeletingConversationId(null)} onDeleteHistory={() => { void confirmDelete(false) }} onDeleteHistoryAndMemories={() => { void confirmDelete(true) }} />}
      {feedbackOpen && <FeedbackDialog candidates={activeConversation?.candidates ?? []} onCancel={() => setFeedbackOpen(false)} onSubmit={(choice, chosenReason, notChosenReason) => { void saveFeedback(choice, chosenReason, notChosenReason) }} />}
    </div>
  )
}
