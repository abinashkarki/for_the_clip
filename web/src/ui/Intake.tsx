// Stage lists and progress lines for source intake, shared by the Library, the pre-flight dialog and the activity tray.
import { useState } from 'react'
import type { IntakeStage, SourceSummary } from '../api/types'
import { formatDuration, headline, rangeLabel } from '../state/intake'
import { Icon } from './Icon'
import { Spinner, cx } from './kit'
import '../styles/intake.css'

function Glyph({ status, stalled }: { status: IntakeStage['status']; stalled?: boolean }) {
  if (status === 'active' && stalled) return <Icon name="warn" size={14} />
  if (status === 'active') return <Spinner size={13} />
  if (status === 'done') return <Icon name="check" size={14} />
  if (status === 'failed') return <Icon name="warn" size={14} />
  if (status === 'skipped') return <Icon name="minus" size={14} />
  return <span className="glyph-dot" />
}

const STATUS_WORD: Record<IntakeStage['status'], string> = { pending: 'Not started', active: 'In progress', done: 'Done', failed: 'Failed', skipped: 'Skipped' }

export function StageList({ stages, className }: { stages: IntakeStage[]; className?: string }) {
  return (
    <ol className={cx('stage-list', className)} aria-label="Steps">
      {stages.map((s) => (
        <li key={s.id} className={cx('stage-row', `is-${s.status}`, s.stalled && 'is-stalled')}>
          <span className="stage-glyph" title={STATUS_WORD[s.status]}>
            <Glyph status={s.status} stalled={s.stalled} />
            <span className="sr-only">{STATUS_WORD[s.status]}</span>
          </span>
          <span className="stage-name">{s.label}</span>
          <span className="stage-detail">{s.status === 'pending' ? '' : s.detail}</span>
          {s.status === 'active' && s.fraction != null && <span className="stage-pct">{Math.round(s.fraction * 100)}%</span>}
          {s.stalled && (
            <span className="stage-stall" role="alert">
              <Icon name="warn" size={13} /> No progress for a while. {s.stall_reason}
            </span>
          )}
        </li>
      ))}
    </ol>
  )
}

/** What is running now, each step with its own numbers, the overall bar, and a stall warning when nothing moves. */
export function SourceProgress({ source, expandable = false }: { source: SourceSummary; expandable?: boolean }) {
  const [open, setOpen] = useState(false)
  const h = headline(source)
  const stages = source.stages ?? []
  const running = [h.lead, ...h.also].filter((s): s is IntakeStage => Boolean(s))
  return (
    <div className={cx('src-progress', h.stalled && 'is-stalled')}>
      {h.waiting && <div className="src-line"><span className="src-stage">{h.text}</span></div>}
      {running.map((s) => (
        <div key={s.id} className={cx('src-step', s.stalled && 'is-stalled')}>
          <div className="src-step-heading">
            {s.stalled ? <Icon name="warn" size={12} /> : <Spinner size={11} />}
            <span className="src-stage">{s.label}</span>
            {s.fraction != null && <span className="src-pct">{Math.round(s.fraction * 100)}%</span>}
          </div>
          <div className={cx('src-stage-bar', s.fraction == null && 'is-indeterminate')}
            role="progressbar" aria-label={`${s.label} progress`} aria-valuemin={0} aria-valuemax={100}
            aria-valuenow={s.fraction == null ? undefined : Math.round(s.fraction * 100)}>
            <span style={s.fraction == null ? undefined : { width: `${Math.max(0, Math.min(100, Math.round(s.fraction * 100)))}%` }} />
          </div>
          {s.detail && <span className="src-detail">{s.detail}</span>}
        </div>
      ))}
      {h.stalled && (
        <div className="src-stall" role="alert">
          <Icon name="warn" size={14} />
          <span>No progress for over a minute. {h.reason}</span>
        </div>
      )}
      {(source.range || source.quality === 'small' || source.duration) && (
        <div className="src-facts">
          {source.duration ? <span>{formatDuration(source.duration)}</span> : null}
          {source.range && <span>Part: {rangeLabel(source.range)}</span>}
          {source.quality === 'small' && <span>Small download</span>}
        </div>
      )}
      {expandable && stages.length > 0 && !h.waiting && (
        <>
          <button className="mini-link src-toggle" type="button" aria-expanded={open} onClick={() => setOpen((v) => !v)}>
            {open ? 'Hide steps' : 'Show all steps'}
          </button>
          {open && <StageList stages={stages} />}
        </>
      )}
    </div>
  )
}
