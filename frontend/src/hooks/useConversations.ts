import { useCallback, useEffect, useMemo, useState } from 'react'

import { createConversation, type ChatConversation, type ChatMessage } from '../types'

export const STORAGE_KEY = 'personal-decision-agent.conversations.v1'

type NewMessage = Pick<ChatMessage, 'role' | 'content'> & Partial<Pick<ChatMessage, 'id' | 'createdAt' | 'trace'>>

function loadConversations(): ChatConversation[] {
  try {
    const saved: unknown = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? '[]')
    return Array.isArray(saved)
      ? (saved as ChatConversation[]).sort((left, right) => right.updatedAt.localeCompare(left.updatedAt))
      : []
  } catch {
    return []
  }
}

function updateConversation(
  conversations: ChatConversation[],
  conversationId: string,
  update: (conversation: ChatConversation) => ChatConversation,
): ChatConversation[] {
  return conversations
    .map((conversation) => conversation.id === conversationId ? update(conversation) : conversation)
    .sort((left, right) => right.updatedAt.localeCompare(left.updatedAt))
}

export function useConversations() {
  const [conversations, setConversations] = useState<ChatConversation[]>(loadConversations)
  const [activeConversationId, setActiveConversationId] = useState<string | null>(() => loadConversations()[0]?.id ?? null)

  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(conversations))
    } catch {
      // Conversation state remains usable if browser storage is unavailable or full.
    }
  }, [conversations])

  const activeConversation = useMemo(
    () => conversations.find((conversation) => conversation.id === activeConversationId) ?? null,
    [activeConversationId, conversations],
  )

  const newConversation = useCallback((title = '新对话') => {
    const conversation = createConversation(title)
    setConversations((current) => [conversation, ...current])
    setActiveConversationId(conversation.id)
    return conversation
  }, [])

  const appendMessage = useCallback((conversationId: string, message: NewMessage) => {
    const completeMessage: ChatMessage = {
      id: message.id ?? crypto.randomUUID(),
      role: message.role,
      content: message.content,
      createdAt: message.createdAt ?? new Date().toISOString(),
    }
    setConversations((current) => updateConversation(current, conversationId, (conversation) => ({
      ...conversation,
      title: conversation.title === '新对话' && conversation.messages.length === 0 && completeMessage.role === 'user'
        ? completeMessage.content
        : conversation.title,
      updatedAt: completeMessage.createdAt,
      messages: [...conversation.messages, completeMessage],
    })))
    return completeMessage
  }, [])

  const setDecisionId = useCallback((conversationId: string, decisionId: string) => {
    setConversations((current) => updateConversation(current, conversationId, (conversation) => ({
      ...conversation,
      decisionId,
      updatedAt: new Date().toISOString(),
    })))
  }, [])

  const setCandidates = useCallback((conversationId: string, candidates: string[]) => {
    setConversations((current) => updateConversation(current, conversationId, (conversation) => ({ ...conversation, candidates })))
  }, [])

  const updateMessage = useCallback((conversationId: string, messageId: string,
    update: (message: ChatMessage) => ChatMessage) => {
    /** 增量更新正在执行的 Trace 卡片，而不影响同一会话里的其他历史消息。 */
    setConversations((current) => updateConversation(current, conversationId, (conversation) => ({
      ...conversation,
      updatedAt: new Date().toISOString(),
      messages: conversation.messages.map((message) => message.id === messageId ? update(message) : message),
    })))
  }, [])

  const decisionIds = useCallback((conversationId: string) => {
    const conversation = conversations.find((item) => item.id === conversationId)
    if (!conversation) return []
    return [...new Set([conversation.decisionId, ...conversation.messages.map((message) => message.trace?.decisionId)].filter((item): item is string => Boolean(item)))]
  }, [conversations])

  const deleteConversation = useCallback((conversationId: string) => {
    setConversations((current) => current.filter((conversation) => conversation.id !== conversationId))
    setActiveConversationId((current) => current === conversationId ? null : current)
  }, [])

  return {
    conversations,
    activeConversation,
    activeConversationId,
    selectConversation: setActiveConversationId,
    newConversation,
    appendMessage,
    updateMessage,
    setDecisionId,
    setCandidates,
    decisionIds,
    deleteConversation,
  }
}
