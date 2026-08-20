type DeleteConversationDialogProps = {
  title: string
  onCancel: () => void
  onDeleteHistory: () => void
  onDeleteHistoryAndMemories: () => void
}

/** 让用户明确选择只删除对话，还是同时删除仅由该对话产生的可追溯记忆。 */
export function DeleteConversationDialog({ title, onCancel, onDeleteHistory, onDeleteHistoryAndMemories }: DeleteConversationDialogProps) {
  return <div className="dialog-backdrop" role="presentation">
    <section aria-modal="true" className="delete-dialog" role="dialog" aria-label="删除对话确认">
      <h2>删除“{title}”吗？</h2>
      <p>可只删除聊天与执行记录，也可同时删除仅由这段对话产生的关联记忆。</p>
      <div className="dialog-actions">
        <button type="button" onClick={onDeleteHistory}>仅删除聊天历史</button>
        <button className="danger-button" type="button" onClick={onDeleteHistoryAndMemories}>删除聊天历史及关联记忆</button>
        <button type="button" onClick={onCancel}>取消</button>
      </div>
    </section>
  </div>
}
