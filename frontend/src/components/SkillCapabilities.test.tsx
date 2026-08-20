import { render, screen } from '@testing-library/react'
import { expect, it } from 'vitest'

import { SkillCapabilities } from './SkillCapabilities'


it('只展示用户可直接发起的决策，不展示辅助核验和复盘流程', () => {
  render(<SkillCapabilities />)

  expect(screen.queryByText('证据核验')).toBeNull()
  expect(screen.queryByText('决策复盘')).toBeNull()
  expect(screen.getByText('旅行目的地比较')).toBeTruthy()
})
