import { useMemo, useState } from 'react'
import { api, assetUrl } from '../api/client'
import {
  curvePaths,
  fmtCount,
  fmtPercent,
  fmtSeconds,
  groupDrops,
  groupPostsByProject,
  visibleClips,
  type ClipGroup,
  type StatusFilter,
  platformName,
  statusLabel,
  type Plot,
} from '../api/publishLogic'
import { publishApi, type Performance, type Post, type RetentionData } from '../api/publish'
import { formatTime } from '../render/timeline'
import { timeAgo, useAsync, useDocumentTitle } from '../state/hooks'
import { href } from '../state/route'
import { Icon } from '../ui/Icon'
import { Button, EmptyState, Segmented, Spinner, cx, toast } from '../ui/kit'
import '../styles/publish.css'

export function AnalyticsScreen({ id }: { id?: string }) {
  return id ? <ClipAnalytics id={id} /> : <Overview />
}

export function summarizePublishing(list: Post[]) {
  const views = list.flatMap((p) => typeof p.metrics?.views === 'number' ? [p.metrics.views] : [])
  return {
    views: views.length ? views.reduce((sum, value) => sum + value, 0) : null,
    partialViews: views.length > 0 && views.length < list.filter((p) => p.status === 'published').length,
    live: list.filter((p) => p.status === 'published').length,
    attention: list.filter((p) => p.status === 'failed').length,
    scheduled: list.filter((p) => p.status === 'scheduled').length,
    drafts: list.filter((p) => p.status === 'draft').length,
    uploading: list.filter((p) => p.status === 'uploading' || p.rendering).length,
  }
}

export function matchesPublishingFilter(list: Post[], filter: StatusFilter) {
  return filter === 'all' || list.some((p) =>
    filter === 'attention' ? p.status === 'failed' :
    filter === 'live' ? p.status === 'published' :
    filter === 'scheduled' ? p.status === 'scheduled' : p.status === 'draft')
}

// ---- overview of everything published

const COLLAPSED_KEY = 'clippie.publishing.collapsed'

function loadCollapsed(): Set<string> {
  try {
    const raw = JSON.parse(localStorage.getItem(COLLAPSED_KEY) ?? '[]')
    return new Set(Array.isArray(raw) ? raw.filter((x): x is string => typeof x === 'string') : [])
  } catch {
    return new Set()
  }
}

function saveCollapsed(keys: Set<string>) {
  try {
    localStorage.setItem(COLLAPSED_KEY, JSON.stringify([...keys]))
  } catch {
    /* storage unavailable: the state just lasts for this visit */
  }
}

function Overview() {
  useDocumentTitle('Publishing')
  const posts = useAsync<Post[]>(() => publishApi.listPosts(), [], (list) => (list.some((p) => p.rendering || p.status === 'uploading') ? 3000 : 0))
  const [filter, setFilter] = useState<StatusFilter>('all')
  const [query, setQuery] = useState('')
  const [collapsed, setCollapsed] = useState<Set<string>>(loadCollapsed)
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const toggle = (key: string, set: Set<string>, write: (next: Set<string>) => void, persist = false) => {
    const next = new Set(set)
    if (!next.delete(key)) next.add(key)
    write(next)
    if (persist) saveCollapsed(next)
  }
  const all = useMemo(() => posts.data ?? [], [posts.data])
  const clipCount = useMemo(() => new Set(all.map((p) => p.short_id)).size, [all])
  const sections = useMemo(() => groupPostsByProject(all, filter, query), [all, filter, query])
  const countFor = (f: StatusFilter) => groupPostsByProject(all, f).reduce((n, g) => n + g.clips.length, 0)
  const projectCount = useMemo(() => groupPostsByProject(all).filter((g) => !g.deleted).length, [all])
  const totals = summarizePublishing(all)
  const filters: { value: StatusFilter; label: string; count: number }[] = [
    { value: 'all', label: 'All clips', count: clipCount },
    { value: 'attention', label: 'Failed', count: countFor('attention') },
    { value: 'live', label: 'Published', count: countFor('live') },
    { value: 'scheduled', label: 'Scheduled', count: countFor('scheduled') },
    { value: 'drafts', label: 'Drafts', count: countFor('drafts') },
  ]
  return (
    <div className="page">
      <header className="project-head">
        <div>
          <h1 className="project-title">Publishing</h1>
          <div className="muted">Track your posts, see what needs attention, and review performance by project and clip.</div>
        </div>
        <div className="project-actions">
          <a className="btn btn-secondary" href={href.channels()}><Icon name="link" size={16} /> Manage accounts</a>
        </div>
      </header>
      {posts.loading && !posts.data && <div className="skeleton pub-skel" />}
      {posts.error && !posts.data && <EmptyState title="Could not load posts" action={<Button onClick={posts.reload}>Try again</Button>}>{posts.error}</EmptyState>}
      {posts.data && clipCount === 0 && (
        <EmptyState title="Nothing published yet" action={<a className="btn btn-primary" href={href.home()}>Open the library</a>}>
          Open a clip and press Publish to post or schedule it. Connect your accounts on the Channels page first.
        </EmptyState>
      )}
      {clipCount > 0 && (
        <section className="pub-overview" aria-labelledby="pub-clips-heading">
          <div className="pub-overview-intro">
            <div><h2 className="section-title" id="pub-clips-heading">Your posts</h2>
              <p className="muted small">{all.length} post{all.length === 1 ? '' : 's'} across {clipCount} clip{clipCount === 1 ? '' : 's'}{projectCount > 0 ? ` in ${projectCount} project${projectCount === 1 ? '' : 's'}` : ''} · {totals.live} published{totals.attention > 0 ? ` · ${totals.attention} need attention` : ''}</p>
            </div>
            <label className="pub-search"><span className="field-label">Search posts</span>
              <input className="input" type="search" placeholder="Title, project, platform or account" value={query} onChange={(e) => setQuery(e.target.value)} />
            </label>
          </div>
          <div className="pub-filters" role="group" aria-label="Filter clips by post status">
            {filters.map((item) => <button key={item.value} type="button" className={cx('pub-filter', filter === item.value && 'is-active')} aria-pressed={filter === item.value} onClick={() => setFilter(item.value)}>{item.label} <span>{item.count}</span></button>)}
          </div>
          {sections.length === 0 ? <EmptyState title="No matching clips">Try another search or status filter.</EmptyState> : (
            <div className="pub-projects">
              {sections.map((g) => {
                const isCollapsed = collapsed.has(g.key)
                const showAll = expanded.has(g.key)
                const shown = visibleClips(g.clips, showAll)
                const bodyId = `pub-project-${g.key}`
                return (
                  <section key={g.key} className={cx('pub-project', g.deleted && 'is-deleted')} aria-label={g.name}>
                    <header className="pub-project-head">
                      <button type="button" className="pub-project-toggle" aria-expanded={!isCollapsed} aria-controls={bodyId}
                        onClick={() => toggle(g.key, collapsed, setCollapsed, true)}>
                        <span className={cx('pub-project-chevron', !isCollapsed && 'is-open')}><Icon name="chevron" size={16} /></span>
                        <span className="pub-project-name">{g.name}</span>
                      </button>
                      <div className="pub-project-counts muted small">
                        {g.clips.length} clip{g.clips.length === 1 ? '' : 's'}
                        {g.published > 0 && <span className="pub-status-badge is-live">{g.published} published</span>}
                        {g.failed > 0 && <span className="pub-status-badge is-failed">{g.failed} failed</span>}
                        {g.scheduled > 0 && <span className="pub-status-badge">{g.scheduled} scheduled</span>}
                      </div>
                      {g.projectId && <a className="btn btn-ghost btn-sm" href={href.project(g.projectId)}>Open project</a>}
                    </header>
                    {!isCollapsed && (
                      <div id={bodyId}>
                        <ul className="pub-clip-list" aria-label={`Clips in ${g.name}`}>
                          {shown.map((c) => <ClipRow key={c.shortId} clip={c} showProject={g.deleted} />)}
                        </ul>
                        {g.clips.length > 5 ? (
                          <button type="button" className="btn btn-ghost btn-sm pub-show-all" onClick={() => toggle(g.key, expanded, setExpanded)}>
                            {showAll ? 'Show fewer' : `Show all ${g.clips.length}`}
                          </button>
                        ) : null}
                      </div>
                    )}
                  </section>
                )
              })}
            </div>
          )}
        </section>
      )}
    </div>
  )
}

function ClipRow({ clip, showProject }: { clip: ClipGroup<Post>; showProject: boolean }) {
  const g = { ...clip, ...summarizePublishing(clip.list) }
  return (
    <li>
      <a className="pub-clip-row" href={href.analytics(g.shortId)}>
        <div className="pub-account-main">
          <div className="queue-title">{g.title}</div>
          <div className="muted small">{showProject && g.projectName ? `${g.projectName} · ` : ''}{g.list.length} post{g.list.length === 1 ? '' : 's'} · {[...new Set(g.list.map((p) => platformName(p.platform)))].join(', ')}</div>
        </div>
        <div className="pub-row-status">
          {g.attention > 0 && <span className="pub-status-badge is-failed">{g.attention} need attention</span>}
          {g.uploading > 0 && <span className="pub-status-badge">{g.uploading} in progress</span>}
          {g.scheduled > 0 && <span className="pub-status-badge">{g.scheduled} scheduled</span>}
          {g.drafts > 0 && <span className="pub-status-badge">{g.drafts} draft{g.drafts === 1 ? '' : 's'}</span>}
          {g.live > 0 && <span className="pub-status-badge is-live">{g.live} published</span>}
        </div>
        <div className="pub-stat-inline"><strong>{fmtCount(g.views)}</strong><span className="muted small">{g.partialViews ? 'views · partial' : 'views'}</span></div>
        <Icon name="chevron" size={16} />
      </a>
    </li>
  )
}

// ---- per clip

function ClipAnalytics({ id }: { id: string }) {
  const perf = useAsync<Performance>(() => publishApi.performance(id), [id], (d) => (d.posts.some((p) => p.rendering || p.status === 'uploading') ? 3000 : 0))
  const clip = useAsync(() => api.getClip(id).catch(() => null), [id])
  const title = clip.data?.clip.title ?? perf.data?.posts[0]?.title ?? 'Clip'
  useDocumentTitle(`${title} analytics`)
  const [picked, setPicked] = useState<string | null>(null)
  const posts = perf.data?.posts ?? []
  const live = posts.filter((p) => p.status === 'published')
  const selected = live.find((p) => p.id === picked) ?? live[live.length - 1] ?? null

  return (
    <div className="page">
      <nav className="crumbs" aria-label="Breadcrumb">
        <a href={href.analytics()}><Icon name="back" size={15} /> Publishing</a>
      </nav>
      <header className="project-head">
        <div>
          <h1 className="project-title">{title}</h1>
          <div className="muted">
            {perf.data && `${fmtCount(summarizePublishing(posts).views)}${summarizePublishing(posts).partialViews ? ' partial' : ''} views across ${posts.length} post${posts.length === 1 ? '' : 's'}`}
          </div>
        </div>
        <div className="project-actions">
          <a className="btn btn-secondary" href={href.clip(id)}>Open in editor</a>
        </div>
      </header>

      {perf.loading && !perf.data && <div className="skeleton pub-skel" />}
      {perf.error && !perf.data && <EmptyState title="Could not load results" action={<Button onClick={perf.reload}>Try again</Button>}>{perf.error}</EmptyState>}
      {perf.data && posts.length === 0 && (
        <EmptyState title="This clip has not been published" action={<a className="btn btn-primary" href={href.clip(id)}>Open in editor</a>}>
          Press Publish in the editor to post or schedule it. Results appear here an hour after it goes live.
        </EmptyState>
      )}

      {perf.data && posts.length > 0 && (
        <>
          {live.length > 1 && (
            <Segmented
              label="Post"
              value={selected?.id ?? ''}
              onChange={setPicked}
              options={live.map((p) => ({ value: p.id, label: `${platformName(p.platform)} · edit ${p.revision_id.slice(-4)}`, title: p.title }))}
            />
          )}
          {selected ? (
            <><div className="pub-results-heading"><h2 className="section-title">Performance</h2><p className="muted small">{platformName(selected.platform)}{selected.channel_name ? ` · ${selected.channel_name}` : ''}{selected.metrics_at ? ` · Updated ${timeAgo(selected.metrics_at)}` : ' · No metrics yet'}</p></div><PostResults post={selected} current={perf.data.current_revision_id} /></>
          ) : (
            <EmptyState title="Nothing is live yet">Metrics start an hour after a post goes live. Posts below show their progress.</EmptyState>
          )}
          <PostHistory posts={posts} current={perf.data.current_revision_id} onChange={perf.reload} />
        </>
      )}
    </div>
  )
}

function PostResults({ post, current }: { post: Post; current: string | null }) {
  const m = post.metrics
  const cards: { label: string; value: string; hint?: string }[] = [
    { label: 'Views', value: fmtCount(m?.views) },
    { label: 'Average view', value: fmtSeconds(m?.avg_view_duration_s), hint: m?.avg_view_percentage != null ? `${fmtPercent(m.avg_view_percentage)} of the clip` : undefined },
    { label: 'Watch time', value: m?.watch_minutes != null ? `${fmtCount(m.watch_minutes)} min` : 'n/a' },
    { label: 'Likes', value: fmtCount(m?.likes) },
    { label: 'Shares', value: fmtCount(m?.shares) },
    ...(post.platform === 'youtube' ? [{ label: 'New subscribers', value: fmtCount(m?.subscribers_gained) }] : [{ label: 'Saves', value: fmtCount(m?.saves) }]),
  ]
  return (
    <section aria-label={`Results for ${post.title}`} className="pub-results">
      {current && post.revision_id !== current && (
        <div className="pub-banner" role="note">
          <Icon name="restore" size={16} />
          <span>This post used an earlier edit of the clip. The transcript below is that edit, not the current one.</span>
        </div>
      )}
      <ul className="pub-stats">
        {cards.map((c) => (
          <li key={c.label} className="pub-stat">
            <span className="pub-stat-label">{c.label}</span>
            <strong>{c.value}</strong>
            {c.hint && <span className="muted small">{c.hint}</span>}
          </li>
        ))}
      </ul>
      {!m && <p className="muted small">No metrics yet. The first reading arrives an hour after the post goes live.</p>}
      <RetentionPanel post={post} />
    </section>
  )
}

const PLOT: Plot = { w: 760, h: 240, left: 44, right: 14, top: 14, bottom: 30 }

function RetentionPanel({ post }: { post: Post }) {
  const data = useAsync<RetentionData>(() => publishApi.retention(post.id), [post.id])
  const [hover, setHover] = useState<number | null>(null)
  const d = data.data
  if (data.loading && !d) return <div className="skeleton pub-skel" />
  if (data.error && !d) return <p className="bad small">{data.error}</p>
  if (!d || d.points.length === 0)
    return (
      <div className="pub-card">
        <h2 className="section-title">Audience retention</h2>
        <p className="muted small">{post.platform === 'youtube' ? d?.audience === 'not_available_yet'
          ? 'YouTube has no retention curve for this Short yet. Analytics usually lag two to three days, and a Short needs some views before YouTube reports one. Clippie keeps checking.'
          : 'YouTube has not returned a retention curve yet. Analytics usually lag two to three days.' : 'This platform does not share a retention curve. Views, reach, average watch time and skip rate are shown above when available.'}</p>
      </div>
    )
  const drops = groupDrops(d.words, d.drops)
  const duration = d.duration ?? 0
  const { line, area, max } = curvePaths(d.points, PLOT)
  const x0 = PLOT.left
  const x1 = PLOT.w - PLOT.right
  const y0 = PLOT.top
  const y1 = PLOT.h - PLOT.bottom
  const xAt = (ratio: number) => x0 + ratio * (x1 - x0)
  const yAt = (v: number) => y1 - (v / max) * (y1 - y0)
  const dropSet = new Set(d.drops)
  const at = hover != null ? d.words[hover] : null
  const onMove = (e: React.MouseEvent<SVGSVGElement>) => {
    const box = e.currentTarget.getBoundingClientRect()
    const ratio = ((e.clientX - box.left) / box.width * PLOT.w - x0) / (x1 - x0)
    if (!d.words.length || ratio < 0 || ratio > 1) return setHover(null)
    let best = 0
    d.words.forEach((w, i) => {
      if (Math.abs(w.ratio - ratio) < Math.abs(d.words[best].ratio - ratio)) best = i
    })
    setHover(best)
  }
  return (
    <div className="pub-card pub-retention">
      <header className="pub-card-head">
        <div>
          <h2 className="section-title">Where viewers left</h2>
          <p className="muted small">Share of viewers still watching at each moment. Red marks the words on screen when the biggest drops happened.{d.audience === 'all' ? ' Covers all viewers: YouTube has no organic-only curve for this Short yet.' : ''}</p>
        </div>
      </header>
      <svg
        className="pub-chart"
        viewBox={`0 0 ${PLOT.w} ${PLOT.h}`}
        role="img"
        aria-label={`Audience retention for ${post.title}. ${drops.length ? `Largest drop-offs at: ${drops.slice(0, 3).map((g) => `"${g.text}" at ${formatTime(g.start)}`).join(', ')}.` : 'No sharp drop-offs.'}`}
        onMouseMove={onMove}
        onMouseLeave={() => setHover(null)}
      >
        {[0, 0.5, 1].map((v) => (
          <g key={v}>
            <line className="pub-grid" x1={x0} x2={x1} y1={yAt(v)} y2={yAt(v)} />
            <text className="pub-axis" x={x0 - 8} y={yAt(v) + 4} textAnchor="end">{Math.round(v * 100)}%</text>
          </g>
        ))}
        {drops.map((g) => (
          <rect key={g.start} className="pub-drop-band" x={xAt(g.start / duration)} width={Math.max(xAt(g.end / duration) - xAt(g.start / duration), 3)} y={y0} height={y1 - y0} />
        ))}
        <path className="pub-area" d={area} />
        <path className="pub-line" d={line} />
        {[0, 0.25, 0.5, 0.75, 1].map((r) => (
          <text key={r} className="pub-axis" x={xAt(r)} y={PLOT.h - 8} textAnchor={r === 0 ? 'start' : r === 1 ? 'end' : 'middle'}>{formatTime(r * duration)}</text>
        ))}
        {at && (
          <g>
            <line className="pub-cross" x1={xAt(at.ratio)} x2={xAt(at.ratio)} y1={y0} y2={y1} />
            <circle className="pub-dot" cx={xAt(at.ratio)} cy={yAt(at.watch_ratio)} r={4.5} />
          </g>
        )}
      </svg>
      <div className="pub-readout" aria-live="off">
        {at ? <><strong>{Math.round(at.watch_ratio * 100)}%</strong> still watching at {formatTime(at.start)} <span className="muted">· "{at.text}"</span></> : <span className="muted">Hover the chart or a word to read it.</span>}
      </div>

      {d.words.length > 0 && (
        <div className="pub-transcript" role="group" aria-label="Transcript coloured by audience still watching">
          {d.words.map((w) => (
            <button
              key={w.index}
              type="button"
              className={cx('pub-word', dropSet.has(w.index) && 'is-drop', hover === w.index && 'is-hover')}
              style={{ ['--keep' as string]: String(Math.max(0.15, Math.min(w.watch_ratio, 1))) }}
              onMouseEnter={() => setHover(w.index)}
              onFocus={() => setHover(w.index)}
              onBlur={() => setHover(null)}
              title={`${formatTime(w.start)} · ${Math.round(w.watch_ratio * 100)}% watching`}
            >
              {w.text}
            </button>
          ))}
        </div>
      )}

      {drops.length > 0 ? (
        <ol className="pub-drops" aria-label="Biggest drop-offs">
          {[...drops].sort((a, b) => b.loss - a.loss).slice(0, 4).map((g) => (
            <li key={g.start}>
              <span className="pub-drop-time">{formatTime(g.start)}</span>
              <span>"{g.text}"</span>
              <span className="muted small">{Math.round(g.loss * 100)}% of viewers left here</span>
            </li>
          ))}
        </ol>
      ) : (
        <p className="muted small">No sharp drop-offs: viewers left gradually.</p>
      )}
    </div>
  )
}

function PostHistory({ posts, current, onChange }: { posts: Post[]; current: string | null; onChange: () => void }) {
  const [busy, setBusy] = useState<string | null>(null)
  const run = async (post: Post, action: 'sync' | 'retry' | 'publish' | 'delete' | 'resolve') => {
    setBusy(post.id + action)
    try {
      if (action === 'sync') await publishApi.syncPost(post.id)
      if (action === 'retry') await publishApi.retryPost(post.id)
      if (action === 'publish') await publishApi.publishPost(post.id)
      if (action === 'resolve') {
        if (!confirm('Have you checked the platform and confirmed this video was not uploaded? This enables Retry; it does not upload yet.')) return
        await publishApi.resolveUncertain(post.id)
      }
      if (action === 'delete') {
        if (!confirm('Delete this post record? It stays on the platform if it is already there.')) return
        await publishApi.deletePost(post.id)
      }
      onChange()
    } catch (e) {
      toast(e instanceof Error ? e.message : 'That did not work', 'bad')
    } finally {
      setBusy(null)
    }
  }
  return (
    <section aria-labelledby="hist-h">
      <div className="section-head">
        <h2 className="section-title" id="hist-h">Post history</h2>
      </div>
      <ul className="pub-history">
        {[...posts].reverse().map((p) => {
          const label = statusLabel(p)
          return (
            <li key={p.id} className="pub-post">
              <span className={cx('pub-badge', `pub-${p.platform}`)} aria-hidden="true">{p.platform === 'youtube' ? 'YT' : p.platform === 'tiktok' ? 'TT' : 'IG'}</span>
              <div className="pub-account-main">
                <div className="queue-title">{p.title}</div>
                <div className="muted small">
                  {platformName(p.platform)}
                  {p.channel_name ? ` · ${p.channel_name}` : ''} · {p.published_at ? `published ${timeAgo(p.published_at)}` : p.scheduled_at ? `scheduled for ${new Date(p.scheduled_at).toLocaleString()}` : `created ${timeAgo(p.created_at)}`}
                </div>
                {p.export_recovered ? <div className="small pub-ok">The export finished after the interruption. It will be checked again before publishing.</div> : p.error && <div className="bad small">{p.error}</div>}
              </div>
              <span className={cx('pub-rev', current && p.revision_id === current && 'is-current')} title={`Clip revision ${p.revision_id}`}>
                {current && p.revision_id === current ? 'Current edit' : `Earlier edit ${p.revision_id.slice(-4)}`}
              </span>
              <span className={cx('pub-state', `tone-${label.tone}`)}>{label.text}</span>
              <span className="pub-views">{p.metrics ? `${fmtCount(p.metrics.views)} views` : ''}</span>
              <div className="pub-actions">
                {p.export_recovered && p.export_preview_url && <a className="btn btn-secondary btn-sm" href={assetUrl(p.export_preview_url)} target="_blank" rel="noreferrer">Preview export</a>}
                {p.url && <a className="icon-btn" href={p.url} target="_blank" rel="noreferrer" aria-label="Open on the platform" title="Open on the platform"><Icon name="external" size={16} /></a>}
                {p.external_id && p.status !== 'draft' && <Button size="sm" kind="ghost" busy={busy === p.id + 'sync'} onClick={() => void run(p, 'sync')}>{busy === p.id + 'sync' ? <Spinner size={12} /> : 'Refresh'}</Button>}
                {p.status === 'failed' && !p.error?.toLowerCase().includes('upload result is uncertain') && <Button size="sm" onClick={() => void run(p, 'retry')}>{p.export_recovered ? 'Continue publishing' : 'Retry'}</Button>}
                {p.status === 'failed' && p.error?.toLowerCase().includes('upload result is uncertain') && <>
                  {p.platform === 'youtube' && <a href="https://studio.youtube.com/" target="_blank" rel="noreferrer">Check YouTube</a>}
                  <Button size="sm" kind="secondary" onClick={() => void run(p, 'resolve')}>I checked</Button>
                </>}
                {p.status === 'draft' && <Button size="sm" kind="primary" onClick={() => void run(p, 'publish')}>Publish</Button>}
                {p.status !== 'uploading' && !(p.status === 'scheduled' && p.external_id) && <Button size="sm" kind="ghost" icon="trash" aria-label="Delete post" onClick={() => void run(p, 'delete')} />}
              </div>
            </li>
          )
        })}
      </ul>
    </section>
  )
}
