import { useEffect, useState } from 'react'
import { Button } from './kit'

interface RecoveryState { paused: boolean; restored_at: string | null; restart_required?: boolean }

export function RecoveryNotice() {
  const [state, setState] = useState<RecoveryState | null>(null)
  const [reviewed, setReviewed] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  useEffect(() => {
    const controller = new AbortController()
    void fetch('/api/system/recovery', { signal: controller.signal }).then(async response => {
      if (response.ok) setState(await response.json())
    }).catch(() => {})
    return () => controller.abort()
  }, [])
  if (!state?.paused && !state?.restart_required) return null
  const acknowledge = async () => {
    setBusy(true); setError('')
    try {
      const response = await fetch('/api/system/recovery/acknowledge', { method: 'POST',
        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ reviewed }) })
      if (!response.ok) throw new Error('Could not save the review. Please try again.')
      setState(await response.json())
    } catch { setError('Could not save the review. Please try again.') }
    setBusy(false)
  }
  return <section className="sys-note sys-note-warn" role="status" aria-label="Restored data review">
    <h2>Restored data needs review</h2>
    <p>Automatic clip generation and publishing are paused. Restored unfinished clip work needs Resume; restored scheduled uploads are drafts. External uploads and provider charges after the backup were not undone.</p>
    <p><a href="#/">Review projects</a> · <a href="#/analytics">Review publishing</a></p>
    {state.restart_required ? <p>Review saved. Restart Clippie to enable background recovery. Nothing was dispatched by this acknowledgment.</p> : <>
      <label><input type="checkbox" checked={reviewed} onChange={event => setReviewed(event.target.checked)} disabled={busy} /> I reviewed the restored projects and publishing state.</label>
      <Button disabled={!reviewed || busy} busy={busy} onClick={() => void acknowledge()}>Enable background recovery next launch</Button>
    </>}
    {error && <p role="alert">{error}</p>}
  </section>
}
