import { describe, expect, it } from 'vitest'

import { createConversation } from './types'

describe('createConversation', () => {
  it('creates an empty local conversation with the supplied title and generated id', () => {
    const item = createConversation('测试问题')

    expect(item.messages).toEqual([])
    expect(item.title).toBe('测试问题')
    expect(item.id).toEqual(expect.any(String))
    expect(item.id).not.toBe('')
    expect(item.createdAt).toEqual(item.updatedAt)
  })
})
