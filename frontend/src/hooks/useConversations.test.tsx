import { act, renderHook } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { STORAGE_KEY, useConversations } from './useConversations'

afterEach(() => {
  localStorage.clear()
})

describe('useConversations', () => {
  it('restores saved conversations from localStorage', () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify([{
      id: 'saved', title: '已保存的问题', createdAt: '2026-08-18T00:00:00.000Z',
      updatedAt: '2026-08-18T00:00:00.000Z', messages: [],
    }]))

    const { result } = renderHook(() => useConversations())

    expect(result.current.conversations).toHaveLength(1)
    expect(result.current.activeConversation?.title).toBe('已保存的问题')
  })

  it('restores the newest conversation at the top of the history', () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify([
      { id: 'older', title: '较早对话', createdAt: '2026-08-18T00:00:00.000Z', updatedAt: '2026-08-18T00:00:00.000Z', messages: [] },
      { id: 'newer', title: '最新对话', createdAt: '2026-08-19T00:00:00.000Z', updatedAt: '2026-08-19T00:00:00.000Z', messages: [] },
    ]))

    const { result } = renderHook(() => useConversations())

    expect(result.current.conversations.map((item) => item.id)).toEqual(['newer', 'older'])
    expect(result.current.activeConversationId).toBe('newer')
  })

  it('creates and persists a new active conversation', () => {
    const { result } = renderHook(() => useConversations())

    act(() => { result.current.newConversation('新的决定') })

    expect(result.current.activeConversation?.title).toBe('新的决定')
    expect(JSON.parse(localStorage.getItem(STORAGE_KEY) ?? '[]')).toHaveLength(1)
  })

  it('appends a message and retains the decision id', () => {
    const { result } = renderHook(() => useConversations())
    let conversationId = ''

    act(() => { conversationId = result.current.newConversation('新的决定').id })
    act(() => {
      result.current.appendMessage(conversationId, { role: 'assistant', content: '推荐杭州' })
      result.current.setDecisionId(conversationId, 'decision-1')
    })

    expect(result.current.activeConversation?.messages).toMatchObject([{ role: 'assistant', content: '推荐杭州' }])
    expect(result.current.activeConversation?.decisionId).toBe('decision-1')
    expect(JSON.parse(localStorage.getItem(STORAGE_KEY) ?? '[]')[0].decisionId).toBe('decision-1')
  })
})
