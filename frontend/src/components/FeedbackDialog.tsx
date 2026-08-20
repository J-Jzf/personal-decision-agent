import { useState } from 'react'

type FeedbackDialogProps = { candidates: string[]; onCancel: () => void; onSubmit: (choice: string, chosenReason: string, notChosenReason: string) => void }

/** 收集用户真实选择和理由，作为 Episode 与长期偏好的唯一反馈来源。 */
export function FeedbackDialog({ candidates, onCancel, onSubmit }: FeedbackDialogProps) {
  const [choice, setChoice] = useState('')
  const [chosenReason, setChosenReason] = useState('')
  const [notChosenReason, setNotChosenReason] = useState('')
  return <div className="dialog-backdrop" role="presentation"><form className="delete-dialog feedback-dialog" aria-label="提交反馈" onSubmit={(event) => { event.preventDefault(); onSubmit(choice, chosenReason, notChosenReason) }}>
    <h2>提交反馈</h2>
    <label>选择 {candidates.length ? <select required value={choice} onChange={(event) => setChoice(event.target.value)}><option value="">请选择</option>{candidates.map((candidate) => <option key={candidate} value={candidate}>{candidate}</option>)}</select> : <input required value={choice} placeholder="填写你最终选择的项目" onChange={(event) => setChoice(event.target.value)} />}</label>
    <label>选择的理由 <textarea value={chosenReason} onChange={(event) => setChosenReason(event.target.value)} /></label>
    <label>不选择的理由 <textarea value={notChosenReason} onChange={(event) => setNotChosenReason(event.target.value)} /></label>
    <div className="dialog-actions"><button type="submit">保存反馈</button><button type="button" onClick={onCancel}>取消</button></div>
  </form></div>
}
