// Pure helpers for publishing: schedule time zones, Shorts checks, hashtags, retention mapping, chart geometry, formatting.

// ---- schedule and time zones

/** Offset (ms) of `tz` from UTC at the given instant. */
export function zoneOffsetMs(tz: string, at: Date): number {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: tz,
    hourCycle: 'h23',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  }).formatToParts(at)
  const get = (t: string) => Number(parts.find((p) => p.type === t)!.value)
  const asUtc = Date.UTC(get('year'), get('month') - 1, get('day'), get('hour'), get('minute'), get('second'))
  return asUtc - Math.floor(at.getTime() / 1000) * 1000
}

/** Convert a wall-clock `YYYY-MM-DDTHH:mm` in `tz` to the matching UTC instant. Returns null for invalid input. */
export function zonedToUtc(local: string, tz: string): Date | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/.exec(local)
  if (!m) return null
  const guess = Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5])
  let utc = guess - zoneOffsetMs(tz, new Date(guess))
  utc = guess - zoneOffsetMs(tz, new Date(utc)) // second pass settles around daylight-saving changes
  const d = new Date(utc)
  return Number.isNaN(d.getTime()) ? null : d
}

/** `datetime-local` value for the next 18:00 in `tz`, at least a day after `now`. */
export function defaultSchedule(now: Date, tz: string): string {
  const later = new Date(now.getTime() + 24 * 3600 * 1000)
  const parts = new Intl.DateTimeFormat('en-CA', { timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit' }).format(later)
  return `${parts}T18:00`
}

export type ScheduleCheck = { ok: true; at: Date } | { ok: false; error: string }

/** Validate a picked schedule: must be a real time at least five minutes ahead. */
export function checkSchedule(local: string, tz: string, now: Date): ScheduleCheck {
  const at = zonedToUtc(local, tz)
  if (!at) return { ok: false, error: 'Pick a date and time.' }
  if (at.getTime() < now.getTime() + 5 * 60 * 1000) return { ok: false, error: 'Pick a time at least 5 minutes from now, or publish now.' }
  if (at.getTime() > now.getTime() + 180 * 24 * 3600 * 1000) return { ok: false, error: 'Pick a time within the next 6 months.' }
  return { ok: true, at }
}

/** UTC ISO string without milliseconds, as the API expects. */
export const toApiTime = (d: Date) => d.toISOString().replace(/\.\d{3}Z$/, 'Z')

// ---- Shorts checks

export interface Check {
  id: string
  ok: boolean
  text: string
}

export function shortsChecks(input: { duration: number | null; width?: number | null; height?: number | null }): Check[] {
  const out: Check[] = []
  if (input.duration != null) {
    const ok = input.duration <= 180
    out.push({ id: 'length', ok, text: ok ? `${Math.round(input.duration)} s, within the 3 minute Shorts limit` : `${Math.round(input.duration)} s is over the 3 minute Shorts limit` })
  }
  if (input.width && input.height) {
    const ok = input.height >= input.width
    out.push({ id: 'vertical', ok, text: ok ? (input.height > input.width ? 'Vertical frame' : 'Square frame') : 'Horizontal frame: YouTube will not list it as a Short' })
  }
  return out
}

// ---- title, description, hashtags

const STOP = new Set('a an and are as at be but by for from had has have how i if in is it its just of on or our so that the their then there these they this to was we were what when where which who why will with you your do does did not no yes about into than too very can could would should'.split(' '))

export function suggestHashtags(title: string, hook?: string | null): string[] {
  const words: string[] = `${title} ${hook ?? ''}`.toLowerCase().match(/[a-z0-9]{4,}/g) ?? []
  const counts = new Map<string, number>()
  for (const w of words) if (!STOP.has(w)) counts.set(w, (counts.get(w) ?? 0) + 1)
  return [...counts.entries()].sort((a, b) => b[1] - a[1] || words.indexOf(a[0]) - words.indexOf(b[0])).slice(0, 3).map(([w]) => w)
}

export function parseHashtags(text: string): string[] {
  const seen = new Set<string>()
  for (const raw of text.split(/[\s,]+/)) {
    const tag = raw.replace(/^#+/, '').replace(/[^\p{L}\p{N}_]/gu, '')
    if (tag) seen.add(tag)
  }
  return [...seen].slice(0, 30)
}

export const formatHashtags = (tags: string[]) => tags.map((t) => `#${t}`).join(' ')

export interface Defaults {
  title: string
  description: string
  hashtags: string[]
}

export interface SuggestedCopy {
  title?: string
  description?: string
  hashtags?: string[]
}

/** Prefill for the publish form: the copy written at generation when there is one, else derived from the clip. */
export function publishDefaults(clipTitle: string, hook?: string | null, post?: SuggestedCopy | null): Defaults {
  const fallback = clipTitle.trim().slice(0, 100) || 'Untitled clip'
  const title = (post?.title ?? '').trim().slice(0, 100) || fallback
  const tags = parseHashtags((post?.hashtags ?? []).join(' ')).map((t) => t.toLowerCase())
  const written = (post?.description ?? '').trim()
  const line = (hook ?? '').trim()
  const description = written || (line && line.toLowerCase() !== title.toLowerCase() ? line : '')
  return { title, description, hashtags: tags.length ? [...new Set(tags)].slice(0, 5) : suggestHashtags(title, hook) }
}

// ---- retention

export type RetentionPoint = [number, number, number | null]
export interface RetentionWord {
  index: number
  text: string
  start: number
  end: number
  ratio: number
  watch_ratio: number
  relative: number | null
  loss: number
}

function column(points: RetentionPoint[], ratio: number, col: 1 | 2): number | null {
  if (!points.length) return null
  if (ratio <= points[0][0]) return points[0][col]
  for (let i = 1; i < points.length; i++) {
    const a = points[i - 1]
    const b = points[i]
    if (ratio <= b[0]) {
      const lo = a[col]
      const hi = b[col]
      if (lo == null || hi == null) return lo ?? hi
      const span = b[0] - a[0]
      return lo + (hi - lo) * (span ? (ratio - a[0]) / span : 0)
    }
  }
  return points[points.length - 1][col]
}

export const watchRatioAt = (points: RetentionPoint[], ratio: number) => column(points, ratio, 1) ?? 0

/** Same mapping as clippie/publish/retention.py, used by the demo data. */
export function mapRetention(points: RetentionPoint[], words: { text: string; start: number; end: number }[], duration: number) {
  if (!points.length || !words.length || !(duration > 0)) return { words: [] as RetentionWord[], drops: [] as number[] }
  const rows: RetentionWord[] = words.map((w, index) => {
    const first = Math.min(w.start / duration, 1)
    const last = Math.min(w.end / duration, 1)
    const mid = (first + last) / 2
    const rel = column(points, mid, 2)
    return {
      index,
      text: w.text,
      start: w.start,
      end: w.end,
      ratio: first,
      watch_ratio: watchRatioAt(points, mid),
      relative: rel,
      loss: Math.max(watchRatioAt(points, first) - watchRatioAt(points, last), 0),
    }
  })
  const average = rows.reduce((s, r) => s + r.loss, 0) / rows.length
  const floor = Math.max(0.01, 1.5 * average)
  return { words: rows, drops: rows.filter((r) => r.loss >= floor).map((r) => r.index) }
}

/** Merge neighbouring drop-off words into phrases for the callout list. */
export function groupDrops(words: RetentionWord[], drops: number[]): { text: string; start: number; end: number; loss: number }[] {
  const out: { text: string; start: number; end: number; loss: number; last: number }[] = []
  for (const i of [...drops].sort((a, b) => a - b)) {
    const w = words[i]
    if (!w) continue
    const prev = out[out.length - 1]
    if (prev && i - prev.last <= 1) {
      prev.text += ` ${w.text}`
      prev.end = w.end
      prev.loss += w.loss
      prev.last = i
    } else out.push({ text: w.text, start: w.start, end: w.end, loss: w.loss, last: i })
  }
  return out.map(({ last: _l, ...rest }) => (void _l, rest))
}

// ---- chart geometry

export interface Plot {
  w: number
  h: number
  left: number
  right: number
  top: number
  bottom: number
}

export function plotArea(p: Plot) {
  return { x0: p.left, x1: p.w - p.right, y0: p.top, y1: p.h - p.bottom }
}

/** SVG path data for the watch-ratio curve and its filled area. The y axis spans 0 to `max`: 100%, or the next quarter above it when viewers re-watch. */
export function curvePaths(points: RetentionPoint[], p: Plot): { line: string; area: string; max: number } {
  const a = plotArea(p)
  const max = Math.max(1, Math.ceil(Math.max(...points.map((pt) => pt[1])) * 4) / 4)
  if (!points.length) return { line: '', area: '', max }
  const xy = points.map((pt) => [a.x0 + pt[0] * (a.x1 - a.x0), a.y1 - (pt[1] / max) * (a.y1 - a.y0)] as const)
  const line = xy.map(([x, y], i) => `${i ? 'L' : 'M'}${x.toFixed(1)} ${y.toFixed(1)}`).join('')
  const area = `${line}L${xy[xy.length - 1][0].toFixed(1)} ${a.y1}L${xy[0][0].toFixed(1)} ${a.y1}Z`
  return { line, area, max }
}

// ---- formatting

export function fmtCount(n: number | null | undefined): string {
  if (n == null) return 'n/a'
  if (n >= 1e6) return `${(n / 1e6).toFixed(n >= 1e7 ? 0 : 1).replace(/\.0$/, '')}M`
  if (n >= 1e4) return `${Math.round(n / 1e3)}K`
  if (n >= 1e3) return `${(n / 1e3).toFixed(1).replace(/\.0$/, '')}K`
  return String(Math.round(n))
}

export const fmtPercent = (v: number | null | undefined, ratio = false) => (v == null ? 'n/a' : `${Math.round(ratio ? v * 100 : v)}%`)

export function fmtSeconds(s: number | null | undefined): string {
  if (s == null) return 'n/a'
  const t = Math.round(s)
  return t >= 60 ? `${Math.floor(t / 60)}:${String(t % 60).padStart(2, '0')}` : `${t} s`
}

export type Tone = 'ok' | 'warn' | 'bad' | 'info'

export function statusLabel(post: { status: string; rendering?: boolean; export_recovered?: boolean; scheduled_at?: string | null; external_id?: string | null }): { text: string; tone: Tone } {
  if (post.status === 'draft') return { text: post.rendering ? 'Draft, exporting' : 'Draft', tone: 'info' }
  if (post.status === 'failed') return post.export_recovered ? { text: 'Export recovered', tone: 'info' } : { text: 'Needs attention', tone: 'bad' }
  if (post.status === 'published') return { text: 'Published', tone: 'ok' }
  if (post.status === 'uploading') return { text: 'Uploading', tone: 'warn' }
  if (post.rendering) return { text: 'Exporting video', tone: 'warn' }
  if (post.external_id) return { text: 'Uploaded, goes live on schedule', tone: 'info' }
  return { text: post.scheduled_at ? 'Waiting to upload' : 'Publishing', tone: 'warn' }
}

export const platformName = (p: string) => ({ youtube: 'YouTube Shorts', tiktok: 'TikTok', instagram: 'Instagram Reels' } as Record<string, string>)[p] ?? p

// ---- Publishing overview: project -> clip -> posts

export type StatusFilter = 'all' | 'attention' | 'live' | 'scheduled' | 'drafts'

export interface GroupablePost {
  id: string
  short_id: string
  title: string
  platform: string
  channel_name: string | null
  status: string
  created_at: string
  published_at: string | null
  project_id?: string | null
  project_name?: string | null
  project_deleted?: boolean
}

export interface ClipGroup<P extends GroupablePost> {
  shortId: string
  title: string
  list: P[]
  activity: number
  /** Name of the project this clip came from (shown in the "deleted projects" section). */
  projectName: string | null
}

export interface ProjectGroup<P extends GroupablePost> {
  key: string
  projectId: string | null
  name: string
  deleted: boolean
  clips: ClipGroup<P>[]
  published: number
  failed: number
  scheduled: number
  postCount: number
  activity: number
}

export const DELETED_PROJECTS_KEY = '__deleted__'

export function postMatchesFilter(post: { status: string }, filter: StatusFilter): boolean {
  return filter === 'all' || (filter === 'attention' ? post.status === 'failed' :
    filter === 'live' ? post.status === 'published' :
    filter === 'scheduled' ? post.status === 'scheduled' : post.status === 'draft')
}

const stamp = (value: string | null | undefined) => {
  const t = value ? Date.parse(value) : NaN
  return Number.isNaN(t) ? 0 : t
}
const postActivity = (p: GroupablePost) => Math.max(stamp(p.published_at), stamp(p.created_at))

function clipsOf<P extends GroupablePost>(posts: P[]): ClipGroup<P>[] {
  const map = new Map<string, P[]>()
  for (const p of posts) map.set(p.short_id, [...(map.get(p.short_id) ?? []), p])
  return [...map.entries()]
    .map(([shortId, list]) => ({
      shortId,
      list,
      title: list[list.length - 1].title,
      activity: Math.max(...list.map(postActivity)),
      projectName: list.find((p) => p.project_name)?.project_name ?? null,
    }))
    .sort((a, b) => b.activity - a.activity)
}

function searchText(clip: ClipGroup<GroupablePost>, projectName: string) {
  return `${clip.title} ${projectName} ${clip.list.map((p) => `${platformName(p.platform)} ${p.channel_name ?? ''}`).join(' ')}`.toLowerCase()
}

/**
 * Group posts by project. Projects sort by most recent post activity; posts from deleted projects
 * (or with no known project) form one final section. A clip shows when any of its posts matches the
 * status filter and the search text matches the clip, its platforms/accounts or its project name.
 * Projects left with no clips are dropped.
 */
export function groupPostsByProject<P extends GroupablePost>(posts: P[], filter: StatusFilter = 'all', query = ''): ProjectGroup<P>[] {
  const needle = query.trim().toLowerCase()
  const live = new Map<string, P[]>()
  const gone: P[] = []
  for (const p of posts) {
    if (p.project_deleted || !p.project_id) gone.push(p)
    else live.set(p.project_id, [...(live.get(p.project_id) ?? []), p])
  }
  const build = (key: string, projectId: string | null, name: string, deleted: boolean, list: P[]): ProjectGroup<P> | null => {
    const clips = clipsOf(list).filter((c) => c.list.some((p) => postMatchesFilter(p, filter)) &&
      (!needle || searchText(c, deleted ? (c.projectName ?? '') : name).includes(needle)))
    if (!clips.length) return null
    const all = clips.flatMap((c) => c.list)
    return {
      key, projectId, name, deleted, clips,
      published: all.filter((p) => p.status === 'published').length,
      failed: all.filter((p) => p.status === 'failed').length,
      scheduled: all.filter((p) => p.status === 'scheduled').length,
      postCount: all.length,
      activity: Math.max(...clips.map((c) => c.activity)),
    }
  }
  const groups = [...live.entries()]
    .map(([id, list]) => build(id, id, list.find((p) => p.project_name)?.project_name ?? 'Untitled project', false, list))
    .filter((g): g is ProjectGroup<P> => g !== null)
    .sort((a, b) => b.activity - a.activity)
  const deleted = build(DELETED_PROJECTS_KEY, null, 'From deleted projects', true, gone)
  return deleted ? [...groups, deleted] : groups
}

/** Clips to render: the first `limit` unless expanded. */
export function visibleClips<T>(clips: T[], expanded: boolean, limit = 5): T[] {
  return expanded || clips.length <= limit ? clips : clips.slice(0, limit)
}
