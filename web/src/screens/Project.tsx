import { useEffect, useMemo, useRef, useState } from 'react'
import { api, assetUrl } from '../api/client'
import type { ClipSummary, ProjectDetail, ProjectSummary } from '../api/types'
import { statusKind, useAsync, useDocumentTitle } from '../state/hooks'
import { href, setLastProject } from '../state/route'
import { formatTime } from '../render/timeline'
import { Icon } from '../ui/Icon'
import { Button, EmptyState, ProgressBar, ScoreBadge, Segmented, Select, Spinner, cx, toast } from '../ui/kit'
import { SourceProgress } from '../ui/Intake'
import { PublishDialog } from '../editor/PublishDialog'
import { ExportDialog } from './ExportDialog'

type Sort = 'score' | 'duration_asc' | 'duration_desc' | 'title'

export function ProjectScreen({ id }: { id: string }) {
  const project = useAsync<ProjectDetail>(() => api.getProject(id), [id], (d) => (statusKind(d.status) === 'running' || d.sources.some((x) => statusKind(x.status) === 'running') ? 1800 : 0))
  const summaries = useAsync<ProjectSummary[]>(() => api.listProjects({ includeTrashed: true }), [id], (list) => (list.find((x) => x.id === id)?.lifecycle === 'active' && project.data?.sources.some((x) => statusKind(x.status) === 'running') ? 2500 : 0))
  const life = (Array.isArray(summaries.data) ? summaries.data.find((x) => x.id === id) : null) ?? null
  const frozen = life?.lifecycle === 'source_freed'
  const restoringSources = (project.data?.sources ?? []).filter((x) => statusKind(x.status) === 'running')
  const [restoring, setRestoring] = useState(false)
  const [restoreNote, setRestoreNote] = useState<string | null>(null)
  const missingUploads = (project.data?.sources ?? []).filter((x) => x.kind === 'file' && x.freed)
  const sourceIsUpload = missingUploads.length > 0
  const uploadOriginal = async (sourceId: string, file: File) => {
    setRestoring(true)
    setRestoreNote(null)
    try {
      await api.restoreUpload(id, sourceId, file)
      project.reload()
      summaries.reload()
    } catch (e) {
      setRestoreNote(e instanceof Error ? e.message : 'Could not restore the original.')
    } finally { setRestoring(false) }
  }
  const restoreSource = async () => {
    setRestoring(true)
    setRestoreNote(null)
    try {
      await api.restoreSource(id)
      project.reload()
      summaries.reload()
    } catch (e) {
      const msg = e instanceof Error ? e.message : 'Could not restore the source.'
      setRestoreNote(msg)
      toast(msg, 'bad')
    }
    setRestoring(false)
  }
  const frozenTip = 'Restore the source to edit or export'
  useDocumentTitle(project.data?.name ?? 'Project')
  useEffect(() => { setLastProject(id) }, [id])
  const [sort, setSort] = useState<Sort>('score')
  const [minScore, setMinScore] = useState<'0' | '0.6' | '0.8'>('0')
  const [q, setQ] = useState('')
  const [picked, setPicked] = useState<Set<string>>(new Set())
  const [resuming, setResuming] = useState(false)
  const [resumeError, setResumeError] = useState<string | null>(null)
  const resume = async () => {
    setResuming(true)
    setResumeError(null)
    try {
      await api.resumeProject(id)
      project.reload()
    } catch (e) {
      setResumeError(e instanceof Error ? e.message : 'Could not resume this project.')
    }
    setResuming(false)
  }
  const [exporting, setExporting] = useState(false)
  const [publishing, setPublishing] = useState(false)

  const d = project.data
  const kind = d ? statusKind(d.status) : 'running'
  const clips = useMemo(() => {
    if (!d) return []
    const min = Number(minScore)
    const needle = q.trim().toLowerCase()
    const list = d.clips.filter((c) => (c.score ?? 0) >= min && (!needle || c.title.toLowerCase().includes(needle)))
    const by: Record<Sort, (a: ClipSummary, b: ClipSummary) => number> = {
      score: (a, b) => (b.score ?? -1) - (a.score ?? -1),
      duration_asc: (a, b) => a.duration - b.duration,
      duration_desc: (a, b) => b.duration - a.duration,
      title: (a, b) => a.title.localeCompare(b.title),
    }
    return [...list].sort(by[sort])
  }, [d, sort, minScore, q])

  const toggle = (cid: string) =>
    setPicked((s) => {
      const n = new Set(s)
      if (n.has(cid)) n.delete(cid)
      else n.add(cid)
      return n
    })
  const selectable = clips.filter((c) => c.status === 'ready')
  const allShown = selectable.length > 0 && selectable.every((c) => picked.has(c.id))

  return (
    <div className="page">
      <nav className="crumbs" aria-label="Breadcrumb">
        <a href={href.home()}>
          <Icon name="back" size={15} /> Library
        </a>
      </nav>
      <header className="project-head">
        <div>
          <h1 className="project-title">{d?.name ?? 'Loading…'}</h1>
          {d && (
            <div className="muted">
              {kind === 'ready' && (d.status === 'partially_ready' ? `${d.clip_count ?? d.counts?.ready ?? 0} clips ready · some need attention` : `${d.clip_count ?? d.counts?.ready ?? 0} clips ready`)}
              {kind === 'running' && 'Finding clips…'}
              {kind === 'failed' && (d.status === 'needs_attention' ? 'Clips need review' : d.resumable ? 'Paused' : 'Something went wrong')}
              {d.sources[0] && ` · from ${d.sources[0].title}`}
            </div>
          )}
        </div>
        <div className="project-actions">
          <Button icon="upload" disabled={picked.size === 0} onClick={() => setPublishing(true)}>
            {picked.size ? `Publish selected (${picked.size})` : 'Publish selected'}
          </Button>
          <Button kind="primary" icon="download" disabled={picked.size === 0 || frozen} title={frozen ? frozenTip : undefined} onClick={() => setExporting(true)}>
            {picked.size ? `Export selected (${picked.size})` : 'Export selected'}
          </Button>
        </div>
      </header>

      {d && frozen && (
        <section className="lifecycle-banner" aria-label="Source freed">
          <div>
            <strong>Source freed to save space. Clips and post history are kept.</strong>
            {sourceIsUpload ? (
              <p className="muted small">
                Select the original file for each missing video. Clippie checks it matches before restoring it. You need to still have the original.
              </p>
            ) : (
              <p className="muted small">Restoring downloads the video again. Editing and exporting come back when it finishes.</p>
            )}
            {restoreNote && <p className="bad small" role="alert">{restoreNote}</p>}
          </div>
          {sourceIsUpload ? (
            <div>{missingUploads.map((source) => (
              <label key={source.id} className="btn btn-secondary">
                {restoring ? 'Checking original…' : `Choose original for ${source.title}`}
                <input type="file" accept="video/*" disabled={restoring} style={{ display: 'none' }} onChange={(event) => {
                  const file = event.currentTarget.files?.[0]
                  event.currentTarget.value = ''
                  if (file) void uploadOriginal(source.id, file)
                }} />
              </label>
            ))}</div>
          ) : (
            <Button kind="primary" icon="restore" busy={restoring} onClick={restoreSource}>Restore source</Button>
          )}
        </section>
      )}
      {d && !frozen && life?.lifecycle === 'active' && restoringSources.length > 0 && kind !== 'running' && (
        <section className="lifecycle-banner" aria-live="polite" aria-label="Restoring source">
          <div>
            <strong>Restoring source</strong>
            <p className="muted small">Editing and exporting are available once the download finishes.</p>
          </div>
          {restoringSources.map((x) => <SourceProgress key={x.id} source={x} />)}
        </section>
      )}

      {project.error && !d && (
        <EmptyState title="Could not open this project" action={<Button onClick={project.reload}>Try again</Button>}>
          {project.error}
        </EmptyState>
      )}

      {d && kind === 'running' && (
        <section className="processing" aria-live="polite">
          <div className="proc-head">
            <Spinner size={18} />
            <strong>{d.progress.stage || 'Preparing clips'}</strong>
            <span className="muted">{d.progress.basis === 'recorded_tasks' ? d.progress.label : `${Math.round(d.progress.fraction * 100)}%`}</span>
          </div>
          {d.progress.basis !== 'recorded_tasks' && <ProgressBar value={d.progress.fraction} label="Project progress" />}
          {d.clips.length === 0 && <div className="grid clips">{[0, 1, 2, 3, 4].map((i) => <div key={i} className="skeleton clip-skel" />)}</div>}
        </section>
      )}

      {d && (kind === 'failed' || d.status === 'partially_ready') && (
        <EmptyState
          title={d.status === 'needs_attention' ? 'Review the clips below' : d.resumable ? 'This project is paused' : 'This project needs attention'}
          action={d.resumable
            ? <Button kind="primary" busy={resuming} onClick={resume}>Resume</Button>
            : <a className="btn btn-secondary" href={href.home()}>Back to library</a>}
        >
          {d.error ?? 'The server reported an error while generating clips.'}
          {resumeError && <p className="bad small">{resumeError}</p>}
        </EmptyState>
      )}

      {d && (kind === 'ready' || d.status === 'needs_attention' || d.clips.length > 0) && (
        <>
          <div className="toolbar" role="search">
            <label className="search">
              <Icon name="search" size={16} />
              <input className="input" placeholder="Search clip titles" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search clip titles" />
            </label>
            <Segmented
              label="Minimum score"
              value={minScore}
              onChange={setMinScore}
              options={[
                { value: '0', label: 'All' },
                { value: '0.6', label: '60+' },
                { value: '0.8', label: '80+' },
              ]}
            />
            <Select
              label="Sort"
              value={sort}
              onChange={setSort}
              options={[
                { value: 'score', label: 'Highest score' },
                { value: 'duration_asc', label: 'Shortest first' },
                { value: 'duration_desc', label: 'Longest first' },
                { value: 'title', label: 'Title A to Z' },
              ]}
            />
            <label className="check">
              <input
                type="checkbox"
                checked={allShown}
                onChange={() => setPicked(allShown ? new Set() : new Set(selectable.map((c) => c.id)))}
              />
              Select all shown
            </label>
          </div>
          {clips.length === 0 ? (
            <EmptyState title="No clips match">Try a lower score filter or clear the search.</EmptyState>
          ) : (
            <div className="grid clips">
              {clips.map((c, i) => (
                <ClipCard key={c.id} clip={c} locked={frozen || restoringSources.length > 0} rank={i + 1} picked={picked.has(c.id)} onPick={() => toggle(c.id)} onResume={resume} resuming={resuming} />
              ))}
            </div>
          )}
        </>
      )}

      {publishing && (
        <PublishDialog
          items={d!.clips.filter((c) => picked.has(c.id)).map((c) => ({ clipId: c.id, title: c.title, revisionId: c.revision_id, duration: c.duration, hook: c.post?.hook_line || null, post: c.post ?? null }))}
          onClose={() => setPublishing(false)}
        />
      )}
      {exporting && (
        <ExportDialog
          items={d!.clips.filter((c) => picked.has(c.id)).map((c) => ({ clipId: c.id, title: c.title, revisionId: c.revision_id }))}
          onClose={() => setExporting(false)}
        />
      )}
    </div>
  )
}

function ClipCard({ clip, rank, picked, onPick, onResume, resuming, locked }: { locked: boolean; clip: ClipSummary; rank: number; picked: boolean; onPick: () => void; onResume: () => void; resuming: boolean }) {
  const [hover, setHover] = useState(false)
  const vid = useRef<HTMLVideoElement>(null)
  useEffect(() => {
    const v = vid.current
    if (!v) return
    if (hover) void v.play().catch(() => {})
    else v.pause()
  }, [hover])
  const canOpen = !locked && (clip.status === 'ready' || Boolean(clip.revision_id))
  const media = <>
    {clip.thumbnail_url && <img src={clip.thumbnail_url} alt="" loading="lazy" />}
    {clip.preview_url && hover && <video ref={vid} src={assetUrl(clip.preview_url)} muted loop playsInline preload="auto" autoPlay />}
    <span className="cc-score"><ScoreBadge score={clip.score} /></span>
    <span className="cc-rank">#{rank}</span>
    {clip.duration != null && <span className="cc-dur">{formatTime(clip.duration)}</span>}
    {clip.score_reasons.length > 0 && <div className="cc-reasons"><ul>{clip.score_reasons.slice(0, 3).map((r) => <li key={r}>{r}</li>)}</ul></div>}
  </>
  return (
    <article
      className={cx('clip-card', picked && 'is-picked', hover && 'is-hover')}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      onFocus={() => setHover(true)}
      onBlur={() => setHover(false)}
    >
      {canOpen ? <a className="cc-media" href={href.clip(clip.id)} aria-label={`Edit ${clip.title}`}>{media}</a> : <div className="cc-media" title={locked ? 'Restore the source to edit this clip' : undefined}>{media}</div>}
      <button className={cx('cc-pick', picked && 'is-on')} disabled={clip.status !== 'ready' || locked} title={locked ? 'Restore the source to export this clip' : undefined} onClick={onPick} aria-pressed={picked} aria-label={picked ? `Deselect ${clip.title}` : `Select ${clip.title}`}>
        {picked && <Icon name="check" size={14} />}
      </button>
      <div className="cc-body">
        {canOpen ? <a className="cc-title" href={href.clip(clip.id)}>{clip.title}</a> : <span className="cc-title" title={locked ? 'Restore the source to edit this clip' : undefined}>{clip.title}</span>}
        <div className="muted small">{clip.format ? clip.format.replace(/_/g, ' ') : 'Clip'}</div>
        {clip.status_message && <p className="muted small">{clip.status_message}</p>}
        {clip.held_review && <details className="small">
          <summary>Inspect editorial review</summary>
          <p><strong>{clip.held_review.title}</strong></p>
          {clip.held_review.hook_line && <p>Hook: {clip.held_review.hook_line}</p>}
          <ul>{clip.held_review.issues.map((issue, i) => <li key={i}><strong>{issue.severity === 'critical' ? 'Needs revision' : 'Review note'}:</strong> {issue.detail}</li>)}</ul>
          <p className="muted">Held for revision. Inspecting this review does not approve the edit for export.</p>
        </details>}
        {clip.status_action?.id === 'resume_project' && <Button size="sm" busy={resuming} onClick={onResume}>{clip.status_action.label}</Button>}
      </div>
    </article>
  )
}
