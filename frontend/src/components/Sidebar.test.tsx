import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { Sidebar } from './Sidebar'


describe('Sidebar', () => {
  it('shows only the first nine characters of each history title followed by ellipsis', () => {
    render(<Sidebar
      activeConversationId="newest"
      conversations={[
        { id: 'newest', title: '上海和桂林这个周末旅游怎么选择', createdAt: '2026-08-19T00:00:00Z', updatedAt: '2026-08-19T00:00:00Z', messages: [] },
      ]}
      onDeleteConversation={() => undefined}
      onNewConversation={() => undefined}
      onSelectConversation={() => undefined}
    />)

    expect(screen.getByRole('button', { name: '上海和桂林这个周末...' })).toBeTruthy()
  })
})
