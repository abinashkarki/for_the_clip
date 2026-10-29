import { useEffect, useId, useRef, useState } from 'react'
import { downloadProgress, formatModelBytes, setupAction, setupApi, setupStatus, type ModelSetup, type SetupModel, type SetupModelId } from '../api/setup'
import { Button, ProgressBar, Spinner, cx, toast } from '../ui/kit'
import '../styles/system.css'
import '../styles/model-setup.css'

const errorMessage = (error: unknown) => error instanceof Error ? error.message : 'Model setup is unavailable. Please try again.'

/** Self-contained: reads setup directly, without waiting for the slower health endpoint. */
export function ModelSetupSection({ introductory = true }: { introductory?: boolean }) {
  const headingId = useId()
  const [snapshot, setSnapshot] = useState<ModelSetup | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [pending, setPending] = useState<SetupModelId | null>(null)
  const [batchPending, setBatchPending] = useState(false)
  const [includeVision, setIncludeVision] = useState(true)
  const batchRef = useRef<(action: 'start' | 'resume' | 'cancel') => void>(() => {})
  const retryRef = useRef<() => void>(() => {})
  const actionRef = useRef<(id: SetupModelId, cancel: boolean) => void>(() => {})

  useEffect(() => {
    let mounted = true
    let latest: ModelSetup | null = null
    let timer: ReturnType<typeof setTimeout> | undefined
    let readPending: Promise<void> | null = null
    let mutationPending = false
    const controller = new AbortController()
    const clearTimer = () => { if (timer !== undefined) clearTimeout(timer); timer = undefined }
    const apply = (next: ModelSetup) => {
      latest = next
      if (mounted) { setSnapshot(next); setError(null) }
    }
    const schedule = () => {
      clearTimer()
      if (mounted && (latest?.active || latest?.batch?.state === 'running') && !mutationPending) timer = setTimeout(() => { void read() }, 1000)
    }
    const read = (): Promise<void> => {
      if (!mounted || mutationPending) return Promise.resolve()
      if (readPending) return readPending
      clearTimer()
      setLoading(true)
      readPending = (async () => {
        let succeeded = false
        try {
          const next = await setupApi.models(controller.signal)
          if (mounted) { apply(next); succeeded = true }
        } catch (e) {
          if (mounted) setError(errorMessage(e))
        } finally {
          readPending = null
          if (mounted) setLoading(false)
          if (succeeded) schedule()
        }
      })()
      return readPending
    }
    retryRef.current = () => { void read() }
    actionRef.current = (id, cancel) => {
      if (mutationPending || !mounted) return
      mutationPending = true
      clearTimer()
      setPending(id)
      setError(null)
      void (async () => {
        // Let an in-flight poll finish before submitting a mutation.
        await readPending
        if (!mounted) return
        try {
          const next = await (cancel ? setupApi.cancel(id, controller.signal) : setupApi.install(id, controller.signal))
          if (mounted) {
            apply(next)
            toast(cancel ? 'Cancellation requested' : 'Model preparation started', 'info')
          }
        } catch (e) {
          if (mounted) setError(errorMessage(e))
        } finally {
          mutationPending = false
          if (mounted) { setPending(null); schedule() }
        }
      })()
    }
    batchRef.current = action => {
      if (mutationPending || !mounted || !latest) return
      mutationPending = true
      clearTimer()
      setBatchPending(true)
      setError(null)
      const selections = Object.fromEntries(latest.models.filter(m => !m.ready && (m.id !== 'vision' || includeVision)).map(m => [m.id, m.terms_identity ?? '']))
      void (async () => {
        await readPending
        if (!mounted) return
        try {
          const next = action === 'cancel' ? await setupApi.cancelBatch(controller.signal) : action === 'resume' ? await setupApi.resumeBatch(controller.signal) : await setupApi.batch(selections, controller.signal)
          if (mounted) apply(next)
        } catch (e) { if (mounted) setError(errorMessage(e)) }
        finally { mutationPending = false; if (mounted) { setBatchPending(false); schedule() } }
      })()
    }
    void read()
    return () => { mounted = false; clearTimer(); controller.abort() }
  }, [includeVision])

  const selected = snapshot?.models.filter(m => !m.ready && (m.id !== 'vision' || includeVision)) ?? []
  const batch = snapshot?.batch
  const running = batch?.state === 'running'
  const resumable = batch?.state === 'failed' || batch?.state === 'interrupted'
  const requiredReady = snapshot?.models.filter(model => model.required).every(model => model.ready)
  return (
    <section className="sys-section model-setup" aria-labelledby={headingId}>
      <div className="section-head">
        <h2 className="section-title" id={headingId}>Local model setup</h2>
        {snapshot && <span className="muted small">{requiredReady ? 'Required models are ready' : 'Prepare the required local models to get started'}</span>}
      </div>
      {introductory && <p className="muted">Prepare Whisper for transcription and embeddings for finding matching moments. Vision is optional. These models run locally. Downloads come from the model publisher; your source media is not uploaded during preparation.</p>}
      {loading && !snapshot && <div className="sys-loading"><Spinner /> Checking local models</div>}
      {error && <div className="sys-note sys-note-warn model-setup-error" role="alert"><span>{error}</span><Button size="sm" onClick={() => retryRef.current()} disabled={loading || pending !== null}>Retry status check</Button></div>}
      {snapshot && <>
        {!snapshot.desktop && <p className="sys-note">Downloads are prepared on the machine running Clippie.</p>}
        <ul className="sys-local model-setup-list">
          {snapshot.models.map(model => <li className="sys-local-row" key={model.id}>
            <strong>{model.label} {model.ready ? '✓' : ''}</strong>
            <p className="muted small">{model.description} · {model.ready ? setupStatus(model) : `Download ${formatModelBytes(model.remaining_download_bytes ?? model.download_bytes)}`} · <a className="sys-link" href={model.terms_url} target="_blank" rel="noreferrer">Publisher terms</a></p>
            {!model.ready && (!model.managed || !model.dependencies_installed) && <p className="sys-note sys-note-warn small">{!model.managed ? 'Managed outside app setup; update its configured location.' : 'A required runtime is missing; repair or reinstall Clippie.'}</p>}
          </li>)}
        </ul>
        {!running && !resumable && selected.length > 0 && <>
          <p className="muted small">Remaining downloads: {formatModelBytes(selected.reduce((n, m) => n + (m.remaining_download_bytes ?? m.download_bytes), 0))} · Estimated free space needed: {formatModelBytes(selected.reduce((n, m) => n + m.required_disk_bytes, 0))}</p>
          <Button kind="primary" disabled={batchPending || !!snapshot.active || selected.some(m => !m.managed || !m.dependencies_installed)} onClick={() => batchRef.current('start')}>Accept terms &amp; download {selected.length === 3 ? 'all 3' : selected.length}</Button>
          <p className="muted small">Clicking accepts the linked publisher terms for the selected models.</p>
        </>}
        {running && <div role="status"><p>Preparing {Math.min(batch.completed.length + 1, Object.keys(batch.selections).length)} of {Object.keys(batch.selections).length} · {snapshot.models.find(m => m.id === batch.current)?.label ?? 'Starting'}</p>{snapshot.models.filter(m => m.id === batch.current).map(m => <div key={m.id}><p className="small">{m.message}</p>{m.state === 'downloading' && <><ProgressBar value={downloadProgress(m) ?? 0} label={`${m.label} download`} /><span className="muted small">{formatModelBytes(m.bytes_received)} of {formatModelBytes(m.bytes_total)}</span></>}</div>)}<Button size="sm" disabled={batchPending} onClick={() => batchRef.current('cancel')}>Cancel setup</Button></div>}
        {resumable && <div><p className="muted small">Remaining downloads: {formatModelBytes(selected.reduce((n, m) => n + (m.remaining_download_bytes ?? m.download_bytes), 0))} · Estimated free space needed: {formatModelBytes(selected.reduce((n, m) => n + m.required_disk_bytes, 0))}</p><p className="sys-note sys-note-warn">{batch.error ?? 'Setup stopped when Clippie closed. Verified models are retained.'}</p><Button disabled={batchPending || !!snapshot.active} onClick={() => batchRef.current('resume')}>{batch.state === 'failed' ? 'Retry remaining' : 'Resume remaining'}</Button> <Button disabled={batchPending || !!snapshot.active || selected.some(m => !m.managed || !m.dependencies_installed)} onClick={() => batchRef.current('start')}>Accept current terms &amp; start again</Button></div>}
        <details><summary>Details and individual model controls</summary>
          <label><input type="checkbox" checked={includeVision} disabled={!!running || batchPending || pending !== null} onChange={e => setIncludeVision(e.target.checked)} /> Include optional vision model</label>
          <ul className="sys-local model-setup-list">
            {snapshot.models.map(model => <ModelRow key={model.id} model={model} active={running ? 'batch' : snapshot.active} pending={pending} onAction={(id, cancel) => actionRef.current(id, cancel)} />)}
          </ul>
        </details>
      </>}
    </section>
  )
}

function ModelRow({ model, active, pending, onAction }: {
  model: SetupModel
  active: string | null
  pending: SetupModelId | null
  onAction: (id: SetupModelId, cancel: boolean) => void
}) {
  const [accepted, setAccepted] = useState(false)
  const termsId = useId()
  const action = setupAction(model, active, accepted, pending !== null)
  const progress = downloadProgress(model)
  return (
    <li className="sys-local-row">
      <div className="sys-local-main model-setup-title">
        <strong>{model.label}</strong><span className="muted small">{model.required ? 'Required' : 'Optional'}</span>
      </div>
      <p className="muted small">{model.description}</p>
      <span className={cx('sys-status', model.ready && 'is-ok', model.error && 'is-bad', model.ready && !model.verified && 'is-external')}><i />{setupStatus(model)}</span>
      {!model.ready && <p className="muted small">Download: {formatModelBytes(model.download_bytes)} · Free space needed, including temporary files: {formatModelBytes(model.required_disk_bytes)}</p>}
      {model.message && <p className="small model-setup-message">{model.message}</p>}
      {model.state === 'downloading' && <div className="model-setup-progress">
        {progress !== null && <ProgressBar value={progress} label={`${model.label} download`} />}
        <span className="muted small">{formatModelBytes(model.bytes_received)} received{model.bytes_total > 0 ? ` of ${formatModelBytes(model.bytes_total)}` : ''}</span>
      </div>}
      {model.error && <p className="sys-note sys-note-warn small" role="alert">{model.error}</p>}
      {!model.dependencies_installed && <p className="sys-note sys-note-warn small">This installation is missing a required app dependency. Repair or reinstall Clippie, then check setup again.</p>}
      {!model.managed && !model.ready && <p className="muted small">This model is managed outside setup. Update its configured location or repair your app installation before downloading here.</p>}
      {action.showInstall && model.managed && model.dependencies_installed && <>
        <label className="model-setup-terms" htmlFor={termsId}>
          <input id={termsId} type="checkbox" checked={accepted} onChange={e => setAccepted(e.target.checked)} disabled={pending !== null || active !== null} />
          <span>I accept the publisher’s <a className="sys-link" href={model.terms_url} target="_blank" rel="noreferrer">model terms</a>.</span>
        </label>
        <div><Button kind={model.required ? 'primary' : 'secondary'} size="sm" disabled={!action.canInstall} busy={pending === model.id} onClick={() => onAction(model.id, false)}>{action.label}</Button></div>
      </>}
      {action.showCancel && <div><Button size="sm" disabled={!action.canCancel} busy={pending === model.id} onClick={() => onAction(model.id, true)}>{model.state === 'cancelling' ? 'Cancelling…' : 'Cancel preparation'}</Button></div>}
      {model.state === 'cancelled' && <p className="muted small">Any previously verified model is kept.</p>}
      <a className="sys-link small" href={model.source_url} target="_blank" rel="noreferrer">Publisher download source</a>
    </li>
  )
}
