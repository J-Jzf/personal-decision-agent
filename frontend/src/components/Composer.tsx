import { useState, type KeyboardEvent } from 'react'

type ComposerProps = {
  disabled: boolean
  onSend: (query: string) => void
}

export function Composer({ disabled, onSend }: ComposerProps) {
  const [value, setValue] = useState('')
  const send = () => {
    const query = value.trim()
    if (!query || disabled) return
    setValue('')
    onSend(query)
  }
  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault()
      send()
    }
  }

  return (
    <form className="composer" onSubmit={(event) => { event.preventDefault(); send() }}>
      <label className="sr-only" htmlFor="decision-query">你的决策问题</label>
      <textarea
        id="decision-query"
        onChange={(event) => setValue(event.target.value)}
        onKeyDown={handleKeyDown}
        placeholder="输入你想一起判断的问题…"
        value={value}
      />
      <button disabled={disabled || !value.trim()} type="submit">{disabled ? '分析中…' : '发送'}</button>
    </form>
  )
}
