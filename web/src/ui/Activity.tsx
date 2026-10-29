// The activity tray: one place for everything running in the background, with a count in the header.
import { useEffect, useRef, useState } from 'react'
import type { ActivityItem } from '../api/types'
import { startActivityPolling, useActivity } from '../state/activity'
import { badge, formatEta, sortActivity } from '../state/intake'
import { href, navigate } from '../state/route'
import { Icon } from './Icon'
import { StageList } from './Intake'
import { Button, IconButton, ProgressBar, Spinner, cx, toast } from './kit'
import '../styles/intake.css'

export function ActivityButton() {
  const { data, open, setOpen } = useActivity()
  const wrap = useRef<HTMLDivElement>(null)
  useEffect(() => startActivityPolling(), [])
  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      if (wrap.current && !wrap.current.contains(e.target as Node)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open, setOpen])
  const { count, attention } = badge(data)
  const label = count ? `Background activity, ${count} running` : attention ? 'Background activity, needs attention' : 'Background activity'
  return (
    <div className="activity" ref={wrap}>
      <button className={cx('activity-btn', open && 'is-open')} aria-haspopup="dialog" aria-expanded={open} aria-label={label} title={label} onClick={() => setOpen(!open)}>
        <Icon name="activity" size={18} />
        <span className="activity-label">Activity</span>
        {count > 0 && <span className="activity-badge" aria-hidden="true">{count}</span>}
        {count === 0 && attention && <span className="activity-badge is-attention" aria-hidden="true" />}
      </button>
      {open && <ActivityPanel />}
    </div>
  )
}

function ActivityPanel() {
  const { data, error } = useActivity()
  const items = sortActivity(data?.items ?? [])
  return (
    <div className="activity-panel" role="dialog" aria-label="Background activity">
      <header className="activity-head">
        <h2>Activity</h2>
        <span className="muted small">{data ? (data.running ? `${data.running} running` : 'Nothing running') : 'Loading…'}</span>
      </header>
      {error && <p className="bad small activity-pad" role="status">{data ? 'Connection lost. Showing the last update; reconnecting…' : error}</p>}
      {data && items.length === 0 && (
        <div className="activity-empty">
          <Icon name="check" size={20} />
          <p>Nothing is running. Videos you add, clips being made and exports show up here.</p>
        </div>
      )}
      <ul className="activity-list">
        {items.map((item) => (
          <ActivityRow key={`${item.kind}-${item.id}`} item={item} />
        ))}
      </ul>
    </div>
  )
}

const KIND_ICON = { source: 'film', project: 'wand', export: 'download', reframe: 'frame', publish: 'external' } as const
const KIND_WORD = { source: 'Video', project: 'Clips', export: 'Export', reframe: 'Reframe', publish: 'Publishing' } as const

export function openTarget(item: ActivityItem): void {
  const o = item.open
  if ('url' in o) window.open(o.url, '_blank', 'noopener')
  else if (o.route === 'project') navigate(href.project(o.id))
  else if (o.route === 'clip') navigate(href.clip(o.id))
  else if (o.route === 'publishing') navigate(href.analytics(o.id))
  else navigate(href.home())
  useActivity.getState().setOpen(false)
}

function ActivityRow({ item }: { item: ActivityItem }) {
  const act = useActivity((s) => s.act)
  const [busy, setBusy] = useState<string | null>(null)
  const run = async (id: 'cancel' | 'retry' | 'resume') => {
    setBusy(id)
    const problem = await act(item, id)
    setBusy(null)
    if (problem) toast(problem, 'bad')
  }
  const working = item.state === 'running' || item.state === 'waiting'
  const eta = formatEta(item.eta_seconds)
  const line = item.queue?.message ?? item.message
  const showStages = item.kind === 'source' && item.stages.length > 0 && item.stages.some((s) => s.status !== 'pending')
  return (
    <li className={cx('activity-item', `is-${item.state}`, item.stalled && 'is-stalled')}>
      <div className="activity-top">
        <div className="activity-thumb">
          {item.thumbnail_url ? <img src={item.thumbnail_url} alt="" /> : <Icon name={KIND_ICON[item.kind]} size={18} />}
        </div>
        <div className="activity-main">
          <div className="activity-title">{item.title}</div>
          <div className="activity-kind muted small">
            {KIND_WORD[item.kind]}
            {item.state === 'waiting' && ' · waiting'}
            {item.state === 'attention' && ' · needs attention'}
            {item.state === 'done' && ' · finished'}
          </div>
        </div>
        <div className="activity-actions">
          {item.actions.map((a) => (
            <Button key={a.id} size="sm" kind={a.id === 'cancel' ? 'ghost' : 'secondary'} busy={busy === a.id} onClick={() => run(a.id)}>
              {a.label}
            </Button>
          ))}
          <IconButton icon="external" label={`Open ${item.title}`} size={16} onClick={() => openTarget(item)} />
        </div>
      </div>
      {working && item.fraction != null && item.state !== 'waiting' && (
        <div className="activity-bar">
          <ProgressBar value={item.fraction} label={`${item.title} progress`} />
          <span className="src-pct">{Math.round(item.fraction * 100)}%</span>
        </div>
      )}
      {line && !showStages && <div role="status" className={cx('activity-line small', item.state === 'attention' ? 'bad' : 'muted')}>{item.state === 'running' && <Spinner size={11} />} {line}</div>}
      {showStages && working && <StageList stages={item.stages} />}
      {showStages && !working && line && <div className="activity-line small bad">{line}</div>}
      {item.queue && showStages && !item.stages.some((s) => s.status === 'active') && <div className="activity-line small muted">{item.queue.message}</div>}
      {working && !item.queue && eta && !showStages && <div className="activity-line small muted">{eta}</div>}
      {item.stalled && item.stall_reason && !showStages && (
        <div className="src-stall" role="alert">
          <Icon name="warn" size={14} /> <span>No progress for over a minute. {item.stall_reason}</span>
        </div>
      )}
      {item.diagnostics && <details className="activity-diagnostics small">
        <summary>Diagnostics</summary>
        <p>Job {item.id.slice(0, 8)} · Attempt {item.diagnostics.attempt}</p>
        {item.diagnostics.events.map((event, index) => <div key={`${event.at}-${index}`}>{new Date(event.at * 1000).toLocaleString()} · {event.message} · attempt {event.attempt}</div>)}
      </details>}
    </li>
  )
}
