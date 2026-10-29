import { type DragEvent, useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import type { ClipRange, ProjectSummary, SourceQuality, SourceSummary } from '../api/types'
import { useActivity } from '../state/activity'
import { statusKind, timeAgo, useAsync, useDocumentTitle } from '../state/hooks'
import { href } from '../state/route'
import { useUploads } from '../state/uploads'
import { Icon } from '../ui/Icon'
import { Button, EmptyState, IconButton, ProgressBar, Spinner, cx, toast } from '../ui/kit'
import { SourceProgress } from '../ui/Intake'
import { LinkPreflight } from './LinkPreflight'
import { NewProjectDialog } from './NewProject'
import {
  type LibraryChip, cardBytes, chipCounts, formatBytes, loadChip, saveChip, trashedProjects, visibleProjects,
} from './libraryLogic'

const errText = (e: unknown) => (e instanceof Error ? e.message : 'That did not work')

export function Library() {
  useDocumentTitle('Library')
  const projects = useAsync<ProjectSummary[]>(() => api.listProjects({ includeTrashed: true }), [], (d) => (d.some((p) => statusKind(p.status) === 'running') ? 2500 : 0))
  const sources = useAsync<SourceSummary[]>(() => api.listSources(), [], (d) => (d.some((s) => statusKind(s.status) === 'running') ? 2000 : 0))
  const uploads = useUploads()
  const [dialogFor, setDialogFor] = useState<string | null>(null)
  const [dragging, setDragging] = useState(false)
  const [url, setUrl] = useState('')
  const [preflight, setPreflight] = useState<string | null>(null)
  const [workingOn, setWorkingOn] = useState<string | null>(null)
  const [chip, setChipState] = useState<LibraryChip>(loadChip)
  const [trashView, setTrashView] = useState(false)
  const [cardBusy, setCardBusy] = useState<string | null>(null)
  const setChip = (c: LibraryChip) => {
    setChipState(c)
    setTrashView(false)
    saveChip(c)
  }
  const fileRef = useRef<HTMLInputElement>(null)
  const depth = useRef(0)

  useEffect(() => {
    useUploads.setState({
      onAccepted: (id) => {
        setDialogFor(id)
        sources.reload()
        useActivity.getState().refresh()
      },
    })
    return () => useUploads.setState({ onAccepted: null })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const usedSources = new Set((projects.data ?? []).flatMap((p) => p.source_ids))
  const waiting = (sources.data ?? []).filter((s) => !usedSources.has(s.id) && statusKind(s.status) !== 'failed')
  const failedSources = (sources.data ?? []).filter((s) => !usedSources.has(s.id) && statusKind(s.status) === 'failed')

  /** run a lifecycle action; blocked actions come back as 409 with a message */
  const runCard = async (id: string, fn: () => Promise<unknown>, okText?: string, undo?: { label: string; onClick: () => void }) => {
    setCardBusy(id)
    try {
      await fn()
      if (okText) toast(okText, 'ok', undo)
    } catch (e) {
      toast(errText(e), 'bad')
    }
    setCardBusy(null)
    projects.reload()
  }
  const actions: CardActions = {
    busyId: cardBusy,
    restore: (p) => runCard(p.id, () => api.untrashProject(p.id), `Restored "${p.name}"`),
  }
  const all = projects.data ?? []
  const counts = chipCounts(all)
  const shown = trashView ? trashedProjects(all) : visibleProjects(all, chip)

  const onFiles = (files: FileList | null) => {
    if (!files) return
    Array.from(files).forEach((f) => uploads.start(f))
  }
  const onDragEnter = (e: DragEvent) => {
    if (!e.dataTransfer.types.includes('Files')) return
    depth.current++
    setDragging(true)
  }
  const onDragLeave = () => {
    depth.current = Math.max(0, depth.current - 1)
    if (!depth.current) setDragging(false)
  }
  const onDrop = (e: DragEvent) => {
    e.preventDefault()
    depth.current = 0
    setDragging(false)
    onFiles(e.dataTransfer.files)
  }

  const submitUrl = () => {
    const v = url.trim()
    if (v) setPreflight(v)
  }
  const startUrl = async (opts: { quality: SourceQuality; range?: ClipRange }) => {
    const v = preflight
    setPreflight(null)
    if (!v) return
    setUrl('')
    await uploads.addUrl(v, opts)
    sources.reload()
    useActivity.getState().refresh()
  }
  const act = async (id: string, what: 'cancel' | 'retry') => {
    setWorkingOn(id)
    try {
      await (what === 'cancel' ? api.cancelSource(id) : api.retrySource(id))
    } catch (e) {
      toast(errText(e), 'bad')
    }
    setWorkingOn(null)
    sources.reload()
    useActivity.getState().refresh()
  }

  return (
    <div className="page" onDragEnter={onDragEnter} onDragLeave={onDragLeave} onDragOver={(e) => e.preventDefault()} onDrop={onDrop}>
      <section className="hero">
        <div className="hero-top">
          <div className="hero-copy">
            <h1>Turn long videos into shorts.</h1>
            <p>Drop in a recording. Clippie finds the moments worth keeping, captions them, and frames each one for vertical.</p>
          </div>
          <HeroFan thumbs={(projects.data ?? []).filter((p) => statusKind(p.status) === 'ready' && p.thumbnail_url).slice(0, 3).map((p) => p.thumbnail_url!)} />
        </div>
        <div className={cx('dropzone', dragging && 'is-over')}>
          <div className="dz-frames" aria-hidden="true">
            <span /><span /><span />
          </div>
          <div className="dz-main">
            <div className="dz-title">{dragging ? 'Drop to upload' : 'Drag a video here'}</div>
            <div className="muted">MP4, MOV, MKV or WebM, up to 4 GB. It stays on this computer.</div>
            <div className="dz-actions">
              <Button kind="primary" icon="upload" size="lg" onClick={() => fileRef.current?.click()}>
                Choose a file
              </Button>
              <input ref={fileRef} type="file" accept="video/*,.mkv" multiple hidden onChange={(e) => { onFiles(e.target.files); e.target.value = '' }} />
            </div>
          </div>
          <div className="dz-or" aria-hidden="true"><span>or</span></div>
          <form className="dz-link" onSubmit={(e) => { e.preventDefault(); submitUrl() }}>
            <label htmlFor="url">Paste a video link</label>
            <div className="input-row">
              <Icon name="link" size={16} />
              <input id="url" className="input" value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://youtube.com/watch?v=…" inputMode="url" />
              <Button kind="secondary" type="submit" disabled={!url.trim()}>
                Add
              </Button>
            </div>
          </form>
        </div>
      </section>

      {(uploads.items.length > 0 || waiting.length > 0 || failedSources.length > 0) && (
        <section aria-labelledby="inprog">
          <h2 id="inprog" className="section-title">In progress</h2>
          <ul className="queue">
            {uploads.items.map((u) => (
              <li key={u.id} className="queue-row">
                <div className="queue-icon"><Icon name="upload" size={18} /></div>
                <div className="queue-main">
                  <div className="queue-title">{u.name}</div>
                  {u.state === 'error' ? (
                    <div className="bad small">{u.error}</div>
                  ) : (
                    <>
                      <ProgressBar value={u.progress} label={`Uploading ${u.name}`} />
                      <div className="muted small">{u.state === 'sending' ? 'Finishing up…' : `Uploading · ${Math.round(u.progress * 100)}%`}</div>
                    </>
                  )}
                </div>
                <IconButton icon="x" label={u.state === 'error' ? 'Dismiss' : 'Cancel upload'} onClick={() => uploads.dismiss(u.id)} />
              </li>
            ))}
            {waiting.map((s) => {
              const running = statusKind(s.status) === 'running'
              return (
                <li key={s.id} className="queue-row">
                  <div className="queue-thumb">{s.thumbnail_url ? <img src={s.thumbnail_url} alt="" /> : <Icon name="film" />}</div>
                  <div className="queue-main">
                    <div className="queue-title">{s.title}</div>
                    {running ? <SourceProgress source={s} expandable /> : <div className="muted small">Ready to cut into clips</div>}
                  </div>
                  <div className="queue-actions">
                    <Button kind={running ? 'ghost' : 'primary'} size="sm" icon="wand" onClick={() => setDialogFor(s.id)}>
                      {running ? 'Set up clips' : 'Make clips'}
                    </Button>
                    {running && s.cancellable && <IconButton icon="x" label={`Cancel ${s.title}`} disabled={workingOn === s.id} onClick={() => act(s.id, 'cancel')} />}
                  </div>
                </li>
              )
            })}
            {failedSources.map((s) => (
              <li key={s.id} className="queue-row is-failed">
                <div className={cx('queue-icon', s.status !== 'cancelled' && 'bad')}><Icon name={s.status === 'cancelled' ? 'x' : 'warn'} size={18} /></div>
                <div className="queue-main">
                  <div className="queue-title">{s.title}</div>
                  <div className={s.status === 'cancelled' ? 'muted small' : 'bad small'}>
                    {s.status === 'cancelled' ? 'Cancelled. Downloaded data is kept, so Retry picks up where it stopped.' : (s.error ?? 'This video could not be processed.')}
                  </div>
                </div>
                <div className="queue-actions">
                  {s.retryable && (
                    <Button kind="secondary" size="sm" icon="restore" busy={workingOn === s.id} onClick={() => act(s.id, 'retry')}>
                      Retry
                    </Button>
                  )}
                </div>
              </li>
            ))}
          </ul>
        </section>
      )}

      <section aria-labelledby="projects-h">
        <div className="section-head">
          <h2 id="projects-h" className="section-title">{trashView ? 'Trash' : 'Projects'}</h2>
          {!trashView && all.length > 0 && <span className="muted small">{counts.all} total</span>}
          {trashView && <span className="muted small">Automatic deletion is paused.</span>}
        </div>
        {!trashView && counts.all + counts.trash > 0 && (
          <div className="lib-filters">
            <div className="lib-chips" role="group" aria-label="Filter projects">
              {([['all', 'All'], ['active', 'Active'], ['ready', 'Ready'], ['freed', 'Source freed']] as const).map(([key, label]) => (
                <button key={key} type="button" className={cx('filter-chip', chip === key && 'is-on')} aria-pressed={chip === key} onClick={() => setChip(key)}>
                  {label} <span className="filter-count">{counts[key]}</span>
                </button>
              ))}
            </div>
            {counts.trash > 0 && (
              <button type="button" className="mini-link lib-trash-link" onClick={() => setTrashView(true)}>
                Trash ({counts.trash})
              </button>
            )}
          </div>
        )}
        {trashView && (
          <div className="lib-filters">
            <button type="button" className="mini-link" onClick={() => setTrashView(false)}>
              Back to projects
            </button>
          </div>
        )}
        {projects.error && !projects.data && (
          <EmptyState title="Could not load your projects" action={<Button onClick={projects.reload}>Try again</Button>}>
            {projects.error}
          </EmptyState>
        )}
        {projects.loading && !projects.data && <div className="grid projects">{[0, 1, 2].map((i) => <div key={i} className="skeleton project-skel" />)}</div>}
        {projects.data && !trashView && counts.all === 0 && (
          <EmptyState title="No projects yet">Add a video above and your first clips will show up here.</EmptyState>
        )}
        {projects.data && !trashView && counts.all > 0 && shown.length === 0 && (
          <EmptyState title="Nothing here">No projects match this filter.</EmptyState>
        )}
        {projects.data && trashView && shown.length === 0 && <EmptyState title="Trash is empty">Previously removed projects appear here.</EmptyState>}
        {shown.length > 0 && (
          <div className="grid projects">
            {shown.map((p) => (
              <ProjectCard key={p.id} p={p} actions={actions} />
            ))}
          </div>
        )}
      </section>

      {dragging && (
        <div className="drop-veil" aria-hidden="true">
          <div>
            <Icon name="upload" size={34} />
            <div>Drop to upload</div>
          </div>
        </div>
      )}
      {dialogFor && <NewProjectDialog sourceId={dialogFor} onClose={() => setDialogFor(null)} />}
      {preflight && <LinkPreflight url={preflight} onStart={startUrl} onClose={() => setPreflight(null)} />}
    </div>
  )
}

function HeroFan({ thumbs }: { thumbs: string[] }) {
  if (!thumbs.length) return null
  return (
    <div className="hero-fan" aria-hidden="true">
      {thumbs.map((src, i) => (
        <img key={i} src={src} alt="" className={`fan-${i}`} />
      ))}
    </div>
  )
}

interface CardActions {
  busyId: string | null
  restore: (p: ProjectSummary) => void
}

function ProjectCard({ p, actions }: { p: ProjectSummary; actions: CardActions }) {
  const kind = statusKind(p.status)
  const trashed = p.lifecycle === 'trashed'
  const freed = p.lifecycle === 'source_freed'
  const busy = actions.busyId === p.id
  const bytes = cardBytes(p)
  const inner = (
    <>
      <div className="pc-thumb">
        {p.thumbnail_url && <div className="pc-bg" style={{ backgroundImage: `url("${p.thumbnail_url}")` }} />}
        {p.thumbnail_url && <img src={p.thumbnail_url} alt="" loading="lazy" />}
        <span className={cx('chip', `chip-${kind}`)}>
          {kind === 'running' ? <Spinner size={10} /> : <span className="dot" />}
          {kind === 'ready' ? `${p.clip_count} clip${p.clip_count === 1 ? '' : 's'}` : kind === 'failed' ? 'Failed' : (p.progress?.stage ?? 'Processing')}
        </span>
      </div>
      <div className="pc-body">
        <div className="pc-name">{p.name}</div>
        {kind === 'running' && !trashed ? (
          <div className="pc-progress">
            {p.progress?.basis !== 'recorded_tasks' && <ProgressBar value={p.progress?.fraction ?? 0} label={`${p.name} progress`} />}
            <span className="muted small">{p.progress?.basis === 'recorded_tasks' ? p.progress.label : `${Math.round((p.progress?.fraction ?? 0) * 100)}%`}</span>
          </div>
        ) : (
          <div className="muted small">
            {trashed ? 'Deletion paused' : timeAgo(p.last_activity_at || p.created_at)}
            {bytes > 0 && ` · ${formatBytes(bytes)}`}
          </div>
        )}
        {!trashed && freed && <div className="pc-tag"><Icon name="check" size={12} /> Source freed</div>}
      </div>
    </>
  )
  return (
    <article className={cx('project-card', `is-${kind}`, trashed && 'is-trashed', busy && 'is-busy')}>
      {trashed ? <div className="pc-link">{inner}</div> : <a className="pc-link" href={href.project(p.id)}>{inner}</a>}
      <div className="pc-actions">
        {trashed ? (
          <>
            <Button size="sm" icon="restore" disabled={busy} onClick={() => actions.restore(p)}>Restore</Button>
          </>
        ) : null}
      </div>
    </article>
  )
}
