import type { ChatConversation } from '../types'

type SidebarProps = {
  conversations: ChatConversation[]
  activeConversationId: string | null
  onNewConversation: () => void
  onSelectConversation: (conversationId: string) => void
  onDeleteConversation: (conversationId: string) => void
}

function historyTitle(title: string): string {
  /** 将侧栏会话标题限制为九个字符，避免长问题挤占会话列表。 */
  return title.length > 9 ? `${title.slice(0, 9)}...` : title
}

export function Sidebar({ conversations, activeConversationId, onNewConversation, onSelectConversation, onDeleteConversation }: SidebarProps) {
  return (
    <aside className="sidebar" aria-label="对话历史">
      <button className="new-conversation" type="button" onClick={onNewConversation}>+ 新对话</button>
      <nav className="conversation-list" aria-label="历史对话">
        {conversations.map((conversation) => (
          <div className="conversation-row" key={conversation.id}>
            <button aria-current={conversation.id === activeConversationId ? 'page' : undefined} className="conversation-item" onClick={() => onSelectConversation(conversation.id)} type="button">{historyTitle(conversation.title)}</button>
            <button aria-label={`删除对话：${conversation.title}`} className="delete-conversation" type="button" onClick={() => onDeleteConversation(conversation.id)}>×</button>
          </div>
        ))}
      </nav>
    </aside>
  )
}
