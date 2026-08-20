import type { ChatMessage } from '../types'

type MessageBubbleProps = {
  message: ChatMessage
  onRetry?: () => void
}

function ReportContent({ content }: Pick<ChatMessage, 'content'>) {
  return <div className="message-content">{content.split('\n').map((line, index) => (
    <p key={`${line}-${index}`}>{line.replace(/^##\s?/, '').replace(/^-\s?/, '') || '\u00a0'}</p>
  ))}</div>
}

export function MessageBubble({ message, onRetry }: MessageBubbleProps) {
  const role = message.role === 'error' ? 'assistant' : message.role
  return (
    <article className={`message message-${message.role}`} data-role={role}>
      <div className="message-label">{message.role === 'user' ? '你' : 'Decision Agent'}</div>
      <ReportContent content={message.content} />
      {message.role === 'error' && onRetry && <button className="retry-button" onClick={onRetry} type="button">重试</button>}
    </article>
  )
}
