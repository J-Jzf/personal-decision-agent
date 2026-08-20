import { useEffect, useState } from 'react'

type Field = { key: string; label: string; placeholder?: string; required?: boolean }
type HITLPromptProps = { request: { request_id: string; question: string; fields?: Field[] }; timeoutSeconds: number; onSubmit: (values: Record<string, string>, freeText: string, skip: boolean) => void }

/** 在实时轨迹中呈现模型公开提出的补充问题，支持提交、跳过和自动超时。 */
export function HITLPrompt({ request, timeoutSeconds, onSubmit }: HITLPromptProps) {
  const [values, setValues] = useState<Record<string, string>>({})
  const [freeText, setFreeText] = useState('')
  const [remaining, setRemaining] = useState(timeoutSeconds)
  const [sent, setSent] = useState(false)
  useEffect(() => {
    if (sent) return
    if (remaining <= 0) { setSent(true); onSubmit({}, '', true); return }
    const timer = window.setTimeout(() => setRemaining((current) => current - 1), 1000)
    return () => window.clearTimeout(timer)
  }, [remaining, sent, onSubmit])
  const submit = (skip: boolean) => { if (!sent) { setSent(true); onSubmit(values, freeText, skip) } }
  return <form className="hitl-prompt" onSubmit={(event) => { event.preventDefault(); submit(false) }}>
    <strong>{request.question}</strong><span>可在 {remaining} 秒内补充，或直接跳过。</span>
    {(request.fields ?? []).map((field) => <label key={field.key}>{field.label}<input required={field.required} placeholder={field.placeholder} value={values[field.key] ?? ''} onChange={(event) => setValues((current) => ({ ...current, [field.key]: event.target.value }))} /></label>)}
    <label>其他补充<textarea value={freeText} onChange={(event) => setFreeText(event.target.value)} placeholder="可选：补充任何你在意的条件" /></label>
    <div><button disabled={sent} type="submit">提交并继续</button><button disabled={sent} type="button" onClick={() => submit(true)}>跳过</button></div>
  </form>
}
