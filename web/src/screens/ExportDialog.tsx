import { useEffect, useRef, useState } from 'react'
import { api, assetUrl } from '../api/client'
import type { ExportResult } from '../api/types'
import { formatTime } from '../render/timeline'
import { Icon } from '../ui/Icon'
import { Button, Dialog, ProgressBar, Segmented, Spinner } from '../ui/kit'

export interface ExportItem {
  clipId: string
  title: string
  revisionId: string
}

interface Row {
  item: ExportItem
  state: 'idle' | 'queued' | 'running' | 'done' | 'failed'
  progress: number
  error?: string
  message?: string
  result?: ExportResult
}

export function ExportDialog({ items, onClose, beforeStart }: { items: ExportItem[]; onClose: () => void; beforeStart?: () => Promise<ExportItem[] | void> }) {
  const [quality, setQuality] = useState<'preview' | '1080p'>('1080p')
  const [rows, setRows] = useState<Row[]>(items.map((item) => ({ item, state: 'idle', progress: 0 })))
  const [started, setStarted] = useState(false)
  const alive = useRef(true)
  useEffect(() => {
    alive.current = true
    return () => {
      alive.current = false
    }
  }, [])

  const patch = (i: number, p: Partial<Row>) => setRows((rs) => rs.map((r, k) => (k === i ? { ...r, ...p } : r)))

  const runOne = async (i: number, item: ExportItem) => {
    try {
      patch(i, { state: 'queued', progress: 0, error: undefined })
      const { job_id } = await api.exportClip(item.clipId, { revision_id: item.revisionId, quality })
      for (;;) {
        await new Promise((r) => setTimeout(r, 700))
        if (!alive.current) return
        const j = await api.getJob(job_id)
        if (j.status === 'failed') return patch(i, { state: 'failed', error: j.error ?? 'Export failed' })
        if (j.status === 'done') return patch(i, { state: 'done', progress: 1, result: j.result as unknown as ExportResult })
        patch(i, { state: j.status === 'queued' ? 'queued' : 'running', progress: j.progress, message: j.message })
      }
    } catch (e) {
      patch(i, { state: 'failed', error: e instanceof Error ? e.message : 'Export failed' })
    }
  }

  const start = async () => {
    setStarted(true)
    let list = items
    if (beforeStart) {
      try {
        const fresh = await beforeStart()
        if (fresh) {
          list = fresh
          setRows(fresh.map((item) => ({ item, state: 'queued', progress: 0 })))
        }
      } catch (e) {
        setRows((rs) => rs.map((r) => ({ ...r, state: 'failed', error: e instanceof Error ? e.message : 'Could not save before exporting' })))
        return
      }
    }
    list.forEach((item, i) => void runOne(i, item))
  }

  const busy = started && rows.some((r) => r.state === 'queued' || r.state === 'running')
  const allDone = started && rows.length > 0 && rows.every((r) => r.state === 'done' || r.state === 'failed')
  const single = rows.length === 1

  return (
    <Dialog
      open
      onClose={onClose}
      title={single ? 'Export clip' : `Export ${rows.length} clips`}
      width={single ? 520 : 600}
      footer={
        <>
          <Button kind="ghost" onClick={onClose}>
            {allDone ? 'Close' : 'Cancel'}
          </Button>
          {!started && (
            <Button kind="primary" icon="download" onClick={start}>
              Start export
            </Button>
          )}
        </>
      }
    >
      {!started && (
        <div className="field">
          <span className="field-label">Quality</span>
          <Segmented
            label="Export quality"
            value={quality}
            onChange={setQuality}
            options={[
              { value: 'preview', label: 'Preview 540p', title: 'Fast draft' },
              { value: '1080p', label: 'Full HD 1080p' },
            ]}
          />
          <p className="muted small">
            {quality === 'preview' ? 'A quick draft to check timing and captions. Drafts are never used for publishing.' : 'Final HD render using the imported source. Requires at least 1080p source quality.'}
          </p>
        </div>
      )}
      <ul className="export-list">
        {rows.map((r) => (
          <li key={r.item.clipId} className="export-row">
            <div className="export-main">
              <div className="queue-title">{r.item.title}</div>
              {r.state === 'idle' && <div className="muted small">Ready to export</div>}
              {(r.state === 'queued' || r.state === 'running') && (
                <>
                  <ProgressBar value={r.progress} label={`Exporting ${r.item.title}`} />
                  <div className="muted small">{r.state === 'queued' ? 'Waiting to start…' : r.message || `Rendering · ${Math.round(r.progress * 100)}%`}</div>
                </>
              )}
              {r.state === 'failed' && <div className="bad small">{r.error}</div>}
              {r.state === 'done' && r.result && <div className="muted small">Done · {formatTime(r.result.duration)}</div>}
            </div>
            <div className="export-actions">
              {(r.state === 'queued' || r.state === 'running') && <Spinner />}
              {r.state === 'done' && r.result && (
                <>
                  <a className="btn btn-primary btn-sm" href={assetUrl(r.result.video_url)} download={`${slug(r.item.title)}.mp4`}>
                    <Icon name="download" size={15} /> Video
                  </a>
                  <a className="btn btn-secondary btn-sm" href={assetUrl(r.result.srt_url)} download={`${slug(r.item.title)}.srt`}>
                    Captions (.srt)
                  </a>
                </>
              )}
              {r.state === 'failed' && (
                <Button size="sm" onClick={() => void runOne(rows.indexOf(r), r.item)}>
                  Try again
                </Button>
              )}
            </div>
          </li>
        ))}
      </ul>
      {single && rows[0].state === 'done' && rows[0].result && (
        <video className="export-preview" src={assetUrl(rows[0].result.video_url)} controls playsInline />
      )}
      {busy && <p className="muted small">You can leave this open. Exports keep running on your computer.</p>}
    </Dialog>
  )
}

const slug = (s: string) => s.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 60) || 'clip'
