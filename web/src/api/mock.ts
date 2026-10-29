// In-memory mock of /api/studio, enabled with ?mock=1. Built from docs/v3/examples/studio_clip.json
// (real-shaped) plus generated projects and clips timed against public/mock/source.webm.
import { defaultRegions } from '../render/layout'
import { duration, normalizeTimeline, programWords } from '../render/timeline'
import type { Cut, Segment, SourceWord, Timeline } from '../render/types'
import { addCut } from '../editor/ops'
import { ApiError } from './errors'
import example from './studio_clip.example.json'
import {
  FILLER_WORDS,
  MOCK_REGISTRY,
  MOCK_SOURCE,
  MOCK_SOURCE_ID,
  TRANSCRIPT,
} from './mockData'
import type {
  ActivityItem,
  ActivityResponse,
  ApiWord,
  ClipRange,
  InspectResult,
  IntakeStage,
  QueueInfo,
  SourceQuality,
  BrandKit,
  BrandKitInput,
  ClipDetail,
  ClipSummary,
  JobInfo,
  ProjectDetail,
  ProjectOptions,
  ProjectSummary,
  SourceSummary,
  StorageInfo,
  StudioApi,
} from './types'

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))
const clone = <T>(v: T): T => JSON.parse(JSON.stringify(v))
const now = () => Date.now()
let counter = 100
const uid = (p: string) => `${p}-${(++counter).toString(36)}`

const fail = (status: number, msg: string): never => {
  throw new ApiError(status, msg)
}

// ---- thumbnails (SVG, 9:16, drawn to look like a crop of the mock video)
function thumb(title: string, hue: number, variant = 0): string {
  const lines = wrap(title, 15).slice(0, 3)
  const text = lines
    .map((l, i) => `<text x="135" y="${332 + i * 30}" text-anchor="middle" font-family="Inter,Arial,sans-serif" font-weight="800" font-size="23" fill="#fff" stroke="#000" stroke-width="5" paint-order="stroke">${esc(l.toUpperCase())}</text>`)
    .join('')
  const cx = variant % 2 ? 180 : 100
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 270 480"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="hsl(${hue},38%,20%)"/><stop offset="1" stop-color="hsl(${(hue + 40) % 360},44%,33%)"/></linearGradient></defs><rect width="270" height="480" fill="url(#g)"/><rect y="150" width="270" height="9" fill="hsla(${hue + 20},90%,85%,.45)" transform="rotate(-14 135 150)"/><circle cx="${cx}" cy="215" r="64" fill="hsl(${(hue + 20) % 360},45%,78%)"/><circle cx="${cx + 8}" cy="204" r="9" fill="hsl(${hue},40%,25%)" opacity=".35"/>${text}</svg>`
  return `data:image/svg+xml;utf8,${encodeURIComponent(svg)}`
}
function esc(s: string) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;')
}
function wrap(s: string, n: number): string[] {
  const out: string[] = []
  let cur = ''
  for (const w of s.split(' ')) {
    if ((cur + ' ' + w).trim().length > n && cur) {
      out.push(cur)
      cur = w
    } else cur = (cur + ' ' + w).trim()
  }
  if (cur) out.push(cur)
  return out
}
function sourceThumb(hue = 220): string {
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 320 180"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="hsl(${hue},55%,24%)"/><stop offset="1" stop-color="hsl(${hue + 40},60%,40%)"/></linearGradient></defs><rect width="320" height="180" fill="url(#g)"/><circle cx="80" cy="95" r="31" fill="hsl(${hue + 20},60%,80%)"/><circle cx="240" cy="95" r="31" fill="hsl(${hue + 10},70%,75%)"/></svg>`
  return `data:image/svg+xml;utf8,${encodeURIComponent(svg)}`
}

// ---- clip blueprints
interface Blueprint {
  id?: string
  title: string
  spans: [number, number][] // sentence index ranges, inclusive
  score: number
  reasons: string[]
  template: string
  plan: 'fill' | 'stack' | 'fit' | 'screen_speaker' | 'mixed'
  hook: string | null
  format: string
  hue: number
}

const S = TRANSCRIPT.sentences
const BLUEPRINTS: Blueprint[] = [
  { id: 'short-7d1c', title: "Khanmigo asks how you'd approach it", spans: [[0, 3], [8, 9]], score: 0.82, reasons: ['Opens with a clear question', 'Complete answer within 31s'], template: 'bold_pop', plan: 'mixed', hook: 'This AI tutor refuses to give answers', format: 'explanation', hue: 232 },
  { title: "Don't hand over the answer", spans: [[8, 9]], score: 0.91, reasons: ['Strong, quotable thesis', 'Lands on a clear takeaway', 'High vocal energy'], template: 'karaoke', plan: 'fill', hook: "Don't hand over the answer", format: 'explanation', hue: 335 },
  { title: 'Students stop copying and start thinking', spans: [[2, 4]], score: 0.77, reasons: ['Surprising reversal', 'Self-contained in 17s'], template: 'boxed_word', plan: 'stack', hook: 'Why students stop copying', format: 'small_story', hue: 178 },
  { title: 'A patient teacher for every kid', spans: [[4, 5]], score: 0.68, reasons: ['Emotional close', 'Clear goal statement'], template: 'color_word', plan: 'fit', hook: null, format: 'explanation', hue: 205 },
  { title: 'The results surprised us', spans: [[6, 7]], score: 0.64, reasons: ['Curiosity gap', 'Short and punchy'], template: 'impact_one', plan: 'fill', hook: 'The results surprised us', format: 'small_story', hue: 268 },
  { title: 'Why most tutors fall short', spans: [[1, 2]], score: 0.59, reasons: ['Clear contrast with the status quo'], template: 'podcast_box', plan: 'screen_speaker', hook: null, format: 'exchange', hue: 352 },
  { title: 'Faster homework was never the goal', spans: [[4, 5], [9, 9]], score: 0.52, reasons: ['Reframes the problem'], template: 'phrase_clean', plan: 'fill', hook: 'Faster homework is the wrong goal', format: 'explanation', hue: 222 },
  { title: 'Learning that scales to millions', spans: [[9, 9]], score: 0.41, reasons: ['Short', 'Weak hook, strong ending'], template: 'minimal_lower', plan: 'fit', hook: null, format: 'explanation', hue: 188 },
]

function segmentsFor(spans: [number, number][]): Segment[] {
  return spans.map(([a, b], i) => {
    const start = Math.max(0, Math.round((S[a].start - 0.08) * 100) / 100)
    const end = Math.min(MOCK_SOURCE.duration, Math.round((S[b].end + 0.18) * 100) / 100)
    const cuts: Cut[] = []
    return { id: `s${i + 1}`, source_id: MOCK_SOURCE_ID, start, end, cuts }
  })
}

function shotsFor(plan: Blueprint['plan'], dur: number): Timeline['shots'] {
  const kf = (t: number, x: number, y: number, zoom = 1) => ({ t, x, y, zoom })
  const half = Math.round(dur * 0.55 * 10) / 10
  switch (plan) {
    case 'fit':
      return []
    case 'fill':
      return [{ id: 'sh1', start: 0, end: dur, layout: 'fill', regions: [{ keyframes: [kf(0, 0.26, 0.5), kf(dur, 0.3, 0.5, 1.15)] }] }]
    case 'stack':
      return [
        { id: 'sh1', start: 0, end: half, layout: 'fill', regions: [{ keyframes: [kf(0, 0.27, 0.53)] }] },
        { id: 'sh2', start: half, end: dur, layout: 'stack', regions: [{ keyframes: [kf(half, 0.25, 0.53)] }, { keyframes: [kf(half, 0.75, 0.53)] }] },
      ]
    case 'screen_speaker':
      return [{ id: 'sh1', start: 0, end: dur, layout: 'screen_speaker', regions: [{ keyframes: [kf(0, 0.5, 0.5)] }, { keyframes: [kf(0, 0.75, 0.52, 1.5)] }] }]
    case 'mixed':
      return [
        { id: 'sh1', start: 0, end: half, layout: 'fill', regions: [{ keyframes: [kf(0, 0.27, 0.53), kf(9, 0.3, 0.53, 1.12)] }] },
        { id: 'sh2', start: half, end: dur, layout: 'stack', regions: [{ keyframes: [kf(half, 0.25, 0.53)] }, { keyframes: [kf(half, 0.75, 0.53)] }] },
      ]
  }
}

function buildTimeline(b: Blueprint): Timeline {
  const base = b.id ? (clone(example.timeline) as unknown as Timeline) : null
  const segments = segmentsFor(b.spans)
  if (b.id) {
    // The example's filler cut: the "um" in the second sentence.
    const um = TRANSCRIPT.words.find((w) => w.text === 'um')!
    segments[0].cuts = [{ start: um.start, end: um.end + 0.02, reason: 'filler' }]
  }
  const tl: Timeline = {
    schema_version: 3,
    canvas: { width: 1080, height: 1920, fps: 30, background: { kind: 'blur', color: '#000000' } },
    segments,
    shots: [],
    broll: [],
    captions: { enabled: true, template_id: b.template, overrides: {}, emphasis: [], hidden: [] },
    overlays: [],
    brand_kit_id: null,
    audio: { normalize: true },
    ...(base ? { canvas: base.canvas, captions: base.captions, brand_kit_id: base.brand_kit_id, audio: base.audio } : {}),
  }
  const dur = duration(tl)
  tl.shots = shotsFor(b.plan, dur)
  const hookEnd = Math.min(3, dur)
  if (b.hook) tl.overlays.push({ id: 'o1', kind: 'hook', text: b.hook, start: 0, end: hookEnd, style: 'hook_box' })
  if (b.id) {
    const bar = (example.timeline.overlays as unknown as Timeline['overlays']).find((o) => o.kind === 'progress_bar')
    if (bar) tl.overlays.push(bar)
  }
  return normalizeTimeline(tl)
}

// ---- state
interface ClipState {
  summary: ClipSummary
  projectId: string
  timeline: Timeline
  revId: string
  revisions: { id: string; created_at: string; origin: string }[]
  requests: Map<string, unknown>
}
interface ProjectState {
  id: string
  name: string
  source_ids: string[]
  created_at: string
  startedAt: number
  runMs: number
  failed?: string
  clipIds: string[]
  pendingClips: Blueprint[]
  options?: ProjectOptions
  freed?: boolean
  keepSource?: boolean
  lastActivity?: number
  trashedAt?: number
  sizes?: { source: number; working: number; exports: number }
}
interface SourceState {
  id: string
  title: string
  startedAt: number
  runMs: number
  hue: number
  failed?: string
  /** a web link, so intake shows download and merge steps */
  url?: boolean
  videoSeconds?: number
  quality?: SourceQuality
  range?: ClipRange
  cancelled?: boolean
  /** project cut from this source was freed */
  freed?: boolean
  /** a frozen intake snapshot, for demos and screenshots */
  frozen?: { stages: IntakeStage[]; overall: number; queue?: QueueInfo | null }
}
interface JobState {
  id: string
  title?: string
  kind: string
  startedAt: number
  runMs: number
  result: Record<string, unknown>
  failed?: string
}

const clips = new Map<string, ClipState>()
const projects: ProjectState[] = []
const sources: SourceState[] = []
const jobs = new Map<string, JobState>()
const DAY = 86400_000
let retentionDays = 15
const MODELS_BYTES = 4.2 * 1e9
const OTHER_BYTES = 0.6 * 1e9
const brandKits: BrandKit[] = []
export const mockAssets = new Map<string, string>()

function makeClip(projectId: string, b: Blueprint, idx: number, hueShift = 0): ClipState {
  const id = b.id ?? `${projectId}-c${idx + 1}`
  const timeline = buildTimeline(b)
  const dur = duration(timeline)
  const first = timeline.segments[0].start
  const last = timeline.segments[timeline.segments.length - 1].end
  const revId = `rev-${id.slice(-4)}-1`
  const summary: ClipSummary = {
    id,
    title: b.title,
    score: b.score,
    score_reasons: b.reasons,
    duration: Math.round(dur * 10) / 10,
    revision_id: revId,
    thumbnail_url: thumb(b.title, (b.hue + hueShift) % 360, idx),
    preview_url: `mock/source.webm#t=${first.toFixed(2)},${last.toFixed(2)}`,
    status: 'ready',
    format: b.format,
  }
  return {
    summary,
    projectId,
    timeline,
    revId,
    revisions: [
      { id: revId, created_at: new Date(now() - 3600_000).toISOString(), origin: 'generated' },
    ],
    requests: new Map(),
  }
}

function seed() {
  sources.push({ id: MOCK_SOURCE_ID, title: MOCK_SOURCE.title, startedAt: 0, runMs: 0, hue: 222 })
  const p1: ProjectState = {
    id: 'col-55', name: MOCK_SOURCE.title, source_ids: [MOCK_SOURCE_ID], created_at: new Date(now() - 86400_000 * 0.2).toISOString(),
    startedAt: 0, runMs: 0, clipIds: [], pendingClips: [],
  }
  BLUEPRINTS.forEach((b, i) => {
    const c = makeClip(p1.id, b, i)
    clips.set(c.summary.id, c)
    p1.clipIds.push(c.summary.id)
  })
  projects.push(p1)
  const p2: ProjectState = {
    id: 'col-48', name: 'Podcast ep. 112: building in public', source_ids: [MOCK_SOURCE_ID],
    created_at: new Date(now() - 86400_000 * 2).toISOString(), startedAt: 0, runMs: 0, clipIds: [], pendingClips: [],
  }
  BLUEPRINTS.slice(1, 6).forEach((b, i) => {
    const c = makeClip(p2.id, { ...b, title: ['Ship before you feel ready', 'The first ten users', 'Pricing is a product decision', 'What we got wrong about onboarding', 'Why we stayed small'][i] }, i, 40)
    clips.set(c.summary.id, c)
    p2.clipIds.push(c.summary.id)
  })
  projects.push(p2)
  projects.push({
    id: 'col-61', name: 'Founder keynote 2026', source_ids: ['job-1002'], created_at: new Date(now() - 40_000).toISOString(),
    startedAt: now() - 14_000, runMs: 60_000, clipIds: [], pendingClips: BLUEPRINTS.slice(0, 5),
  })
  sources.push({ id: 'job-1002', title: 'Founder keynote 2026', startedAt: 0, runMs: 0, hue: 262 })
  projects.push({
    id: 'col-40', name: 'Lecture 7: graph traversal', source_ids: ['job-1003'], created_at: new Date(now() - 86400_000 * 5).toISOString(),
    startedAt: 0, runMs: 0, failed: 'Transcription failed: the file has no audio track.', clipIds: [], pendingClips: [],
  })
  sources.push({ id: 'job-1003', title: 'Lecture 7: graph traversal', startedAt: 0, runMs: 0, hue: 210, failed: 'No audio track' })

  const aged = (id: string, name: string, srcId: string, hue: number, ageDays: number, extra: Partial<ProjectState>, bp: Blueprint[]) => {
    sources.push({ id: srcId, title: name, startedAt: 0, runMs: 0, hue, url: true })
    const p: ProjectState = {
      id, name, source_ids: [srcId], created_at: new Date(now() - DAY * (ageDays + 1)).toISOString(), startedAt: 0, runMs: 0,
      clipIds: [], pendingClips: [], lastActivity: now() - DAY * ageDays, ...extra,
    }
    bp.forEach((b, i) => {
      const c = makeClip(id, { ...b, id: undefined }, i, hue)
      clips.set(c.summary.id, c)
      p.clipIds.push(c.summary.id)
    })
    projects.push(p)
  }
  aged('col-30', 'Interview: scaling a support team', 'job-2001', 150, 20, { freed: true, sizes: { source: 0, working: 0, exports: 38e6 } }, BLUEPRINTS.slice(0, 4))
  aged('col-33', 'Conference talk: pricing teardown', 'job-2002', 30, 22, { keepSource: true, sizes: { source: 2.6e9, working: 310e6, exports: 52e6 } }, BLUEPRINTS.slice(2, 6))
  aged('col-22', 'Old webinar recording', 'job-2003', 300, 9, { trashedAt: now() - DAY * 2, sizes: { source: 1.4e9, working: 120e6, exports: 20e6 } }, BLUEPRINTS.slice(0, 3))
  sources.push({ id: 'job-2004', title: 'Team all-hands (upload)', startedAt: 0, runMs: 0, hue: 70, failed: 'The file ended unexpectedly' })

  if (typeof location !== 'undefined' && new URLSearchParams(location.search).get('demo') === 'intake') {
    const dur = 9300
    sources.push({
      id: 'job-long', title: 'The Long Conversation: a patient teacher for every kid', startedAt: now(), runMs: 1e9, hue: 232, url: true, videoSeconds: dur,
      frozen: {
        overall: 0.34,
        stages: [
          mkStage('download', 'Download', 'active', 0.58, '1.83 of 3.13 GB · 3.4 MB/s · ~6 min left', 380),
          mkStage('merge', 'Merge', 'pending', null, null),
          mkStage('preview', 'Browser preview', 'pending', null, null),
          mkStage('transcribe', 'Transcription', 'active', 0.21, '32:40 of 2:35:00 · 3,912 words · ~8 min left', 480),
          mkStage('index', 'Finding moments', 'pending', null, null),
        ],
      },
    })
    sources.push({
      id: 'job-stuck', title: 'Panel discussion (connection dropped)', startedAt: now(), runMs: 1e9, hue: 150, url: true, videoSeconds: 3600,
      frozen: {
        overall: 0.12,
        stages: [
          { ...mkStage('download', 'Download', 'active', 0.31, '0.34 of 1.10 GB · 0 KB/s', null), stalled: true, stall_reason: 'No data has arrived from the video site. The connection may have dropped; Cancel and Retry resumes the download where it stopped.' },
          mkStage('merge', 'Merge', 'pending', null, null),
          mkStage('preview', 'Browser preview', 'pending', null, null),
          mkStage('transcribe', 'Transcription', 'pending', null, null),
          mkStage('index', 'Finding moments', 'pending', null, null),
        ],
      },
    })
    sources.push({ id: 'job-cancelled', title: 'Workshop recording', startedAt: 0, runMs: 12_000, hue: 90, url: true, cancelled: true, videoSeconds: 2400 })
    sources.push({
      id: 'job-second', title: 'Founder Q&A, part two', startedAt: now(), runMs: 1e9, hue: 12, url: true, videoSeconds: 4200,
      frozen: {
        overall: 0,
        queue: { lane: 'network', ahead: 1, position: 2, message: 'Waiting · 1 video ahead (transcribing, ~8 min left)' },
        stages: [
          mkStage('download', 'Download', 'pending', null, null),
          mkStage('merge', 'Merge', 'pending', null, null),
          mkStage('preview', 'Browser preview', 'pending', null, null),
          mkStage('transcribe', 'Transcription', 'pending', null, null),
          mkStage('index', 'Finding moments', 'pending', null, null),
        ],
      },
    })
  }

  const logo = makeLogoDataUrl('#7B8CFF', 'C')
  const logoId = 'asset-seed1'
  mockAssets.set(logoId, logo)
  brandKits.push(
    { id: 'kit-1', name: 'Clippie default', template_id: 'bold_pop', overrides: { colors: { active: '#FFE600' } }, hook_style: 'hook_box', progress_bar: { enabled: true, color: '#FFE600' }, logo_asset_id: logoId, logo_url: logo },
    { id: 'kit-2', name: 'Calm channel', template_id: 'minimal_lower', overrides: {}, hook_style: 'hook_outline', progress_bar: { enabled: false, color: '#FFFFFF' }, logo_asset_id: null },
  )
}

export function makeLogoDataUrl(color: string, letter: string): string {
  if (typeof document === 'undefined') return ''
  const c = document.createElement('canvas')
  c.width = 160
  c.height = 160
  const g = c.getContext('2d')!
  g.fillStyle = color
  g.beginPath()
  g.arc(80, 80, 76, 0, Math.PI * 2)
  g.fill()
  g.fillStyle = '#0b0d11'
  g.font = '800 92px Arial, sans-serif'
  g.textAlign = 'center'
  g.textBaseline = 'middle'
  g.fillText(letter, 80, 88)
  return c.toDataURL('image/png')
}

let seeded = false
function ensure() {
  if (!seeded) {
    seeded = true
    seed()
  }
}

// ---- derived views
const STAGES = ['Transcribing', 'Finding moments', 'Scoring clips', 'Rendering previews']
function progressOf(startedAt: number, runMs: number) {
  const f = runMs ? Math.min((now() - startedAt) / runMs, 1) : 1
  return f
}

function projectStatus(p: ProjectState): { status: string; fraction: number; stage: string } {
  if (p.failed) return { status: 'failed', fraction: 1, stage: 'Failed' }
  const f = progressOf(p.startedAt, p.runMs)
  if (f >= 1) {
    if (p.pendingClips.length) {
      p.pendingClips.forEach((b, i) => {
        const c = makeClip(p.id, b, i, 20)
        clips.set(c.summary.id, c)
        p.clipIds.push(c.summary.id)
      })
      p.pendingClips = []
    }
    return { status: 'ready', fraction: 1, stage: 'Done' }
  }
  return { status: 'running', fraction: f, stage: STAGES[Math.min(Math.floor(f * STAGES.length), STAGES.length - 1)] }
}

const GB = 1e9
const mkStage = (id: IntakeStage['id'], label: string, status: IntakeStage['status'], fraction: number | null, detail: string | null, eta: number | null = null): IntakeStage => ({
  id, label, status, fraction, detail, started_at: null, updated_at: null, eta_seconds: eta, stalled: false,
})
const clockText = (sec: number) => {
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), r = Math.floor(sec % 60)
  return h ? `${h}:${String(m).padStart(2, '0')}:${String(r).padStart(2, '0')}` : `${m}:${String(r).padStart(2, '0')}`
}
const etaText = (sec: number) => (sec < 45 ? `~${Math.max(5, Math.round(sec / 5) * 5)} s left` : sec < 3600 ? `~${Math.max(1, Math.round(sec / 60))} min left` : `~${Math.floor(sec / 3600)} h ${String(Math.round((sec % 3600) / 60)).padStart(2, '0')} min left`)

/** Stage timeline for a source added in the mock: audio first, transcription beside the download, then moments. */
function simulateStages(s: SourceState, f: number): { stages: IntakeStage[]; overall: number } {
  const total = (s.videoSeconds ?? MOCK_SOURCE.duration) * 0.0003 * GB
  const part = (lo: number, hi: number) => Math.min(Math.max((f - lo) / (hi - lo), 0), 1)
  const stages: IntakeStage[] = []
  const left = (hi: number) => Math.max(0, (hi - f) * (s.runMs / 1000))
  if (s.url) {
    const d = part(0, 0.5)
    stages.push(mkStage('download', 'Download', d >= 1 ? 'done' : f > 0 ? 'active' : 'pending', d,
      d >= 1 ? `${(total / GB).toFixed(2)} GB downloaded` : `${((total * d) / GB).toFixed(2)} of ${(total / GB).toFixed(2)} GB · 3.4 MB/s · ${etaText(left(0.5))}`, d < 1 ? left(0.5) : null))
    const m = part(0.5, 0.56)
    stages.push(mkStage('merge', 'Merge', m >= 1 ? 'done' : m > 0 ? 'active' : 'pending', m, m >= 1 ? 'Joined without re-encoding' : null))
  }
  stages.push(mkStage('preview', 'Browser preview', f >= 0.56 ? 'skipped' : 'pending', f >= 0.56 ? 1 : null, f >= 0.56 ? 'Already plays in the browser' : null))
  const t = part(0.08, 0.8)
  const dur = s.videoSeconds ?? MOCK_SOURCE.duration
  stages.push(mkStage('transcribe', 'Transcription', t >= 1 ? 'done' : t > 0 ? 'active' : 'pending', t,
    t >= 1 ? '1,204 words' : `${clockText(dur * t)} of ${clockText(dur)} · ${Math.round(t * 1204).toLocaleString()} words · ${etaText(left(0.8))}`, t < 1 ? left(0.8) : null))
  const i = part(0.8, 0.96)
  stages.push(mkStage('index', 'Finding moments', i >= 1 ? 'done' : i > 0 ? 'active' : 'pending', i, i >= 1 ? '6 candidates' : i > 0 ? 'Choosing candidate moments…' : null))
  return { stages, overall: Math.min(f, 0.99) }
}

function sourceView(s: SourceState): SourceSummary {
  const f = s.failed || s.cancelled ? 1 : progressOf(s.startedAt, s.runMs)
  const ready = !s.failed && !s.cancelled && f >= 1
  const sim = s.frozen ?? (s.runMs ? simulateStages(s, f) : { stages: [], overall: 1 })
  const status = s.failed ? 'failed' : s.cancelled ? 'cancelled' : ready && !s.frozen ? 'ready' : 'ingesting'
  const active = sim.stages.find((x) => x.status === 'active')
  return {
    id: s.id,
    title: s.title,
    status,
    progress: s.failed || s.cancelled ? 0.3 : ready && !s.frozen ? 1 : sim.overall,
    duration: s.videoSeconds ?? MOCK_SOURCE.duration,
    thumbnail_url: sourceThumb(s.hue),
    media_url: MOCK_SOURCE.media_url,
    error: s.failed ?? null,
    message: status === 'ingesting' ? (active ? `${active.label} · ${active.detail ?? ''}` : (s.frozen?.queue?.message ?? 'Waiting to start')) : null,
    kind: s.url ? 'url' : 'file',
    stages: status === 'ingesting' || s.cancelled || s.failed ? sim.stages : [],
    queue: s.frozen?.queue ?? null,
    stalled: sim.stages.some((x) => x.stalled),
    stall_reason: sim.stages.find((x) => x.stalled)?.stall_reason ?? null,
    eta_seconds: active?.eta_seconds ?? null,
    quality: s.quality ?? null,
    range: s.range ?? null,
    cancellable: status === 'ingesting',
    retryable: status === 'failed' || status === 'cancelled',
    freed: Boolean(s.freed),
  }
}

function projectSummary(p: ProjectState): ProjectSummary {
  const st = projectStatus(p)
  const first = p.clipIds.length ? clips.get(p.clipIds[0])! : null
  const top = [...p.clipIds].map((id) => clips.get(id)!).sort((a, b) => (b.summary.score ?? 0) - (a.summary.score ?? 0))[0]
  return {
    id: p.id,
    name: p.name,
    source_ids: p.source_ids,
    status: st.status,
    clip_count: p.clipIds.length,
    created_at: p.created_at,
    thumbnail_url: (top ?? first)?.summary.thumbnail_url ?? sourceThumb(sources.find((s) => s.id === p.source_ids[0])?.hue ?? 220),
    progress: { stage: st.stage, fraction: st.fraction },
    ...lifecycleFields(p, st.status),
  }
}

function sizesOf(p: ProjectState) {
  const base = p.sizes ?? { source: 1.8e9, working: 210e6, exports: 24e6 }
  return p.freed ? { source: 0, working: 0, exports: base.exports } : { ...base }
}
function lastActivityOf(p: ProjectState) {
  return p.lastActivity ?? new Date(p.created_at).getTime()
}
function lifecycleOf(p: ProjectState): ProjectSummary['lifecycle'] {
  return p.trashedAt ? 'trashed' : p.freed ? 'source_freed' : 'active'
}
function lifecycleFields(p: ProjectState, _status: string): Pick<ProjectSummary, 'lifecycle' | 'last_activity_at' | 'keep_source' | 'source_expires_at' | 'size_bytes' | 'trashed_at' | 'purge_at'> {
  const lc = lifecycleOf(p)
  const last = lastActivityOf(p)
  return {
    lifecycle: lc,
    last_activity_at: new Date(last).toISOString(),
    keep_source: Boolean(p.keepSource),
    source_expires_at: null,
    size_bytes: sizesOf(p),
    trashed_at: p.trashedAt ? new Date(p.trashedAt).toISOString() : null,
    purge_at: null,
  }
}
function projectOr404(id: string): ProjectState {
  return projects.find((x) => x.id === id) ?? fail(404, 'Project not found.')
}
function storageInfo(): StorageInfo {
  const live = projects
  const sum = (k: 'source' | 'working' | 'exports') => live.reduce((n, p) => n + sizesOf(p)[k], 0)
  const sources_bytes = sum('source')
  const working_bytes = sum('working')
  const exports_bytes = sum('exports')
  return {
    total_bytes: sources_bytes + working_bytes + exports_bytes + MODELS_BYTES + OTHER_BYTES,
    sources_bytes, working_bytes, exports_bytes,
    models_bytes: MODELS_BYTES,
    other_bytes: OTHER_BYTES,
    reclaimable_bytes: 0,
    retention_days: retentionDays,
  }
}
function clipDetail(c: ClipState): ClipDetail {
  const tl = c.timeline
  const pw = new Map(programWords(tl, TRANSCRIPT.words).map((w) => [w.key, w]))
  const words: ApiWord[] = []
  for (const seg of tl.segments) {
    for (const w of TRANSCRIPT.words) {
      if (w.source_id !== seg.source_id) continue
      const mid = (w.start + w.end) / 2
      if (mid < seg.start || mid >= seg.end) continue
      const p = pw.get(w.key)
      words.push({
        ...w,
        program_start: p ? p.start : null,
        program_end: p ? p.end : null,
        removed: !p,
        emphasis: tl.captions.emphasis.includes(w.key),
        hidden: tl.captions.hidden.includes(w.key),
      })
    }
  }
  return {
    clip: { ...c.summary, duration: Math.round(duration(tl) * 10) / 10, revision_id: c.revId },
    project_id: c.projectId,
    revision_id: c.revId,
    timeline: clone(tl),
    words,
    sources: { [MOCK_SOURCE_ID]: { ...MOCK_SOURCE } },
    revisions: [...c.revisions].reverse(),
  }
}

function commit(c: ClipState, tl: Timeline, origin: string) {
  const prev = c.revId
  c.timeline = normalizeTimeline(clone(tl))
  c.revId = `rev-${c.summary.id.slice(-4)}-${c.revisions.length + 1}`
  c.revisions.push({ id: c.revId, created_at: new Date().toISOString(), origin })
  c.summary = { ...c.summary, revision_id: c.revId, duration: Math.round(duration(c.timeline) * 10) / 10 }
  void prev
  return { revision_id: c.revId, timeline: clone(c.timeline) }
}

function clipOr404(id: string): ClipState {
  ensure()
  const c = clips.get(id)
  if (!c) return fail(404, 'Unknown clip')
  return c
}

function idempotent<T>(c: ClipState, requestId: string, make: () => T): T {
  if (c.requests.has(requestId)) return c.requests.get(requestId) as T
  const out = make()
  c.requests.set(requestId, out)
  return out
}

function checkBase(c: ClipState, base: string) {
  const w = (typeof window === 'undefined' ? globalThis : window) as unknown as { __mockConflictNext?: boolean }
  if (w.__mockConflictNext) {
    w.__mockConflictNext = false
    // simulate someone else saving a new revision
    const tl = clone(c.timeline)
    const hook = tl.overlays.find((o) => o.kind === 'hook')
    if (hook && hook.kind === 'hook') hook.text = hook.text + ' (edited elsewhere)'
    commit(c, tl, 'studio_edit')
    fail(409, 'Base revision is not current')
  }
  if (base !== c.revId) fail(409, 'Base revision is not current')
}

function fillerCuts(tl: Timeline, silenceMin: number): Timeline {
  let out = tl
  for (const seg of tl.segments) {
    const ws: SourceWord[] = TRANSCRIPT.words.filter((w) => w.source_id === seg.source_id && (w.start + w.end) / 2 >= seg.start && (w.start + w.end) / 2 < seg.end)
    ws.forEach((w, i) => {
      if (FILLER_WORDS.has(w.text.toLowerCase().replace(/[^a-z]/g, ''))) out = addCut(out, seg.source_id, w.start - 0.02, w.end + 0.04, 'filler')
      const nx = ws[i + 1]
      if (nx && nx.start - w.end >= silenceMin) out = addCut(out, seg.source_id, w.end + 0.12, nx.start - 0.12, 'silence')
    })
  }
  return out
}

function autoShots(tl: Timeline): Timeline {
  const dur = duration(tl)
  const n = Math.max(2, Math.round(dur / 7))
  const per = dur / n
  const layouts = ['fill', 'stack', 'fill', 'fit', 'fill'] as const
  const shots = Array.from({ length: n }, (_, i) => {
    const layout = layouts[i % layouts.length]
    const start = Math.round(i * per * 100) / 100
    const end = i === n - 1 ? dur : Math.round((i + 1) * per * 100) / 100
    const side = i % 2 ? 0.74 : 0.27
    const regions = defaultRegions(layout, start).map((r, k) => ({
      keyframes: [{ ...r.keyframes[0], x: layout === 'stack' ? (k ? 0.75 : 0.25) : layout === 'fill' ? side : 0.5, y: 0.53 }],
    }))
    return { id: `sh${i + 1}`, start, end, layout, regions }
  })
  return { ...tl, shots }
}

function makeJob(kind: string, runMs: number, result: Record<string, unknown>, title?: string): string {
  const id = uid('job')
  jobs.set(id, { id, kind, title, startedAt: now(), runMs, result })
  return id
}

export const mockApi: StudioApi = {
  async listSources() {
    ensure()
    await sleep(60)
    return sources.map(sourceView)
  },
  upload(file, title, onProgress) {
    ensure()
    let cancelled = false
    const promise = (async () => {
      const steps = 14
      for (let i = 1; i <= steps; i++) {
        await sleep(110)
        if (cancelled) throw new ApiError(0, 'Upload cancelled')
        onProgress(i / steps)
      }
      const id = uid('job')
      sources.push({ id, title: title || file.name.replace(/\.[^.]+$/, ''), startedAt: now(), runMs: 9000, hue: (counter * 47) % 360 })
      return { source_id: id, status: 'running' }
    })()
    return { promise, abort: () => (cancelled = true) }
  },
  async addSource(input) {
    ensure()
    await sleep(120)
    const id = uid('job')
    const label = input.title || (input.url ? input.url.replace(/^https?:\/\/(www\.)?/, '').slice(0, 48) : 'Imported video')
    const span = input.range ? input.range.end - input.range.start : undefined
    sources.push({ id, title: label, startedAt: now(), runMs: 12_000, hue: (counter * 47) % 360, url: Boolean(input.url), quality: input.quality, range: input.range, videoSeconds: span })
    return { source_id: id, status: 'queued' }
  },
  async inspectSource(url) {
    await sleep(650)
    if (/bad|nope/.test(url)) fail(422, 'Could not read that link: Unsupported URL')
    const info: InspectResult = {
      title: 'The Long Conversation: a patient teacher for every kid',
      duration: 9300,
      thumbnail_url: sourceThumb(232),
      uploader: 'Open Classroom',
      options: [
        { id: 'standard', label: '1080p', height: 1080, download_bytes: 3.13e9 },
        { id: 'high', label: '2160p', height: 2160, download_bytes: 7.2e9 },
        { id: 'small', label: '480p · 30 fps', height: 480, download_bytes: 1.12e9 },
      ],
      estimates: {
        download_bytes: { standard: 3.13e9, high: 7.2e9, small: 1.12e9 },
        download_seconds: { standard: 920, high: 2100, small: 330 },
        transcribe_seconds: 372,
      },
    }
    return info
  },
  async cancelSource(id) {
    const s = sources.find((x) => x.id === id) ?? fail(404, 'Source not found.')
    if (s.frozen) s.frozen = { ...s.frozen, stages: s.frozen.stages.map((x) => (x.status === 'active' || x.status === 'pending' ? { ...x, status: 'skipped' as const, detail: 'Cancelled' } : x)) }
    s.cancelled = true
    return sourceView(s)
  },
  async retrySource(id) {
    const s = sources.find((x) => x.id === id) ?? fail(404, 'Source not found.')
    s.cancelled = false
    s.failed = undefined
    s.frozen = undefined
    s.startedAt = now()
    s.runMs = 12_000
    return sourceView(s)
  },
  async getActivity() {
    ensure()
    await sleep(40)
    const items: ActivityItem[] = []
    for (const s of sources) {
      const v = sourceView(s)
      if (v.status !== 'ingesting' && v.status !== 'cancelled' && v.status !== 'failed') continue
      if (v.status !== 'ingesting' && projects.some((p) => p.source_ids.includes(s.id))) continue
      const active = (v.stages ?? []).some((x) => x.status === 'active')
      items.push({
        kind: 'source', id: s.id, title: s.title, state: v.status === 'ingesting' ? (active ? 'running' : 'waiting') : 'attention', status: v.status,
        fraction: v.progress, message: v.status === 'ingesting' ? v.message ?? null : (v.error ?? 'Cancelled'), eta_seconds: v.eta_seconds ?? null,
        stalled: Boolean(v.stalled), stall_reason: v.stall_reason ?? null, thumbnail_url: v.thumbnail_url, stages: v.stages ?? [], queue: v.queue ?? null,
        actions: v.cancellable ? [{ id: 'cancel', label: 'Cancel' }] : [{ id: 'retry', label: 'Retry' }], open: { route: 'library' },
      })
    }
    for (const p of projects) {
      const st = projectStatus(p)
      if (st.status !== 'running') continue
      items.push({
        kind: 'project', id: p.id, title: p.name, state: 'running', status: 'processing', fraction: st.fraction, message: st.stage, eta_seconds: null,
        stalled: false, stall_reason: null, thumbnail_url: null, stages: [], queue: null, actions: [], open: { route: 'project', id: p.id },
      })
    }
    for (const j of jobs.values()) {
      if (j.kind !== 'export') continue
      const f = progressOf(j.startedAt, j.runMs)
      if (f >= 1 && now() - j.startedAt > j.runMs + 600_000) continue
      items.push({
        kind: 'export', id: j.id, title: j.title ?? 'Exporting a clip', state: f >= 1 ? 'done' : 'running', status: f >= 1 ? 'done' : 'running', fraction: f,
        message: f >= 1 ? 'Ready to download' : 'Exporting…', eta_seconds: f >= 1 ? null : Math.round(((1 - f) * j.runMs) / 1000), stalled: false, stall_reason: null,
        thumbnail_url: null, stages: [], queue: null, actions: [], open: typeof j.result.video_url === 'string' ? { url: j.result.video_url } : { route: 'library' },
      })
    }
    const out: ActivityResponse = { items, running: items.filter((i) => i.state === 'running' || i.state === 'waiting').length, attention: items.filter((i) => i.state === 'attention').length }
    return out
  },
  async listProjects(opts) {
    ensure()
    await sleep(80)
    return [...projects]
      .filter((p) => opts?.includeTrashed || !p.trashedAt)
      .sort((a, b) => b.created_at.localeCompare(a.created_at))
      .map(projectSummary)
  },
  async trashProject(_id) {
    return fail(503, 'Source cleanup and project deletion are temporarily unavailable while Clippie protects active work.')
  },
  async untrashProject(id) {
    await sleep(80)
    const p = projectOr404(id)
    if (!p.trashedAt) fail(409, 'This project is not in the trash.')
    p.trashedAt = undefined
    return projectSummary(p)
  },
  async deleteProject(_id) {
    return fail(503, 'Source cleanup and project deletion are temporarily unavailable while Clippie protects active work.')
  },
  async freeSource(_id) {
    return fail(503, 'Source cleanup and project deletion are temporarily unavailable while Clippie protects active work.')
  },
  async keepSource(id, keep) {
    await sleep(60)
    const p = projectOr404(id)
    if (p.freed) fail(409, 'The source is already freed.')
    p.keepSource = keep
    return projectSummary(p)
  },
  async restoreSource(id) {
    await sleep(120)
    const p = projectOr404(id)
    if (!p.freed) fail(409, 'The source has not been freed.')
    const srcs = p.source_ids.map((sid) => sources.find((x) => x.id === sid)).filter((x): x is SourceState => Boolean(x))
    if (srcs.some((x) => !x.url)) fail(422, 'Drop the original file to restore it')
    for (const src of srcs) {
      src.freed = false
      src.startedAt = now()
      src.runMs = 9000
    }
    p.freed = false
    p.lastActivity = now()
    return projectSummary(p)
  },
  async restoreUpload(id, sourceId, _file) {
    const p = projectOr404(id)
    const source = sources.find((x) => x.id === sourceId && p.source_ids.includes(x.id))
    if (!source) return fail(404, 'Source not found.')
    if (!source.freed) return fail(409, 'This source does not need an uploaded original.')
    source.freed = false
    p.freed = p.source_ids.some((sid) => sources.find((x) => x.id === sid)?.freed)
    return projectSummary(p)
  },
  async dismissSource(_id) {
    return fail(503, 'Source cleanup and project deletion are temporarily unavailable while Clippie protects active work.')
  },
  async getStorage() {
    ensure()
    await sleep(60)
    return storageInfo()
  },
  async setRetention(days) {
    await sleep(60)
    if (![0, 7, 15, 30].includes(days)) fail(422, 'Choose 7, 15 or 30 days, or never.')
    retentionDays = days
    return storageInfo()
  },
  async freeStorageNow() {
    return fail(503, 'Source cleanup and project deletion are temporarily unavailable while Clippie protects active work.')
  },
  async createProject(input) {
    ensure()
    await sleep(160)
    const src = sources.find((s) => s.id === input.source_ids[0])
    if (!src) fail(422, 'Unknown source')
    const n = input.options.count === 'auto' ? 6 : Math.min(input.options.count, BLUEPRINTS.length)
    const p: ProjectState = {
      id: uid('col'),
      name: input.name || src!.title,
      source_ids: input.source_ids,
      created_at: new Date().toISOString(),
      startedAt: now(),
      runMs: 14_000,
      clipIds: [],
      pendingClips: BLUEPRINTS.slice(0, n).map((b) => ({ ...b, id: undefined, template: input.options.template_id ?? b.template })),
      options: input.options,
    }
    projects.push(p)
    return projectSummary(p)
  },
  async resumeProject(id) {
    return this.getProject(id)
  },
  async getProject(id) {
    ensure()
    await sleep(60)
    const p = projects.find((x) => x.id === id)
    if (!p) return fail(404, 'Unknown project')
    const st = projectStatus(p!)
    return {
      id: p!.id,
      name: p!.name,
      status: st.status,
      progress: { stage: st.stage, fraction: st.fraction },
      sources: p!.source_ids.map((sid) => sourceView(sources.find((s) => s.id === sid) ?? sources[0])),
      clips: p!.clipIds
        .map((cid) => clips.get(cid)!.summary)
        .sort((a, b) => (b.score ?? -1) - (a.score ?? -1)),
      ...(p!.failed ? { error: p!.failed } : {}),
    } as ProjectDetail
  },
  async getClip(id) {
    const c = clipOr404(id)
    await sleep(90)
    return clipDetail(c)
  },
  async saveTimeline(id, body) {
    const c = clipOr404(id)
    await sleep(140)
    return idempotent(c, body.request_id, () => {
      checkBase(c, body.base_revision)
      const d = duration(body.timeline)
      if (d <= 0.5) fail(422, 'Programme must be longer than 0.5 s')
      return commit(c, body.timeline, 'studio_edit')
    })
  },
  async removeFillers(id, body) {
    const c = clipOr404(id)
    await sleep(500)
    return idempotent(c, body.request_id, () => {
      checkBase(c, body.base_revision)
      return commit(c, fillerCuts(c.timeline, body.silence_min), 'studio_edit')
    })
  },
  async autoReframe(id, body) {
    const c = clipOr404(id)
    await sleep(200)
    const key = `reframe:${c.revId}`
    if (!c.requests.has(key)) {
      c.requests.set(key, true)
      return { job_id: makeJob('auto_reframe', 2200, {}) }
    }
    return idempotent(c, body.request_id, () => {
      checkBase(c, body.base_revision)
      return commit(c, autoShots(c.timeline), 'studio_edit')
    })
  },
  async applyBrandKit(id, body) {
    const c = clipOr404(id)
    await sleep(250)
    return idempotent(c, body.request_id, () => {
      checkBase(c, body.base_revision)
      const kit = brandKits.find((k) => k.id === body.brand_kit_id)
      if (!kit) fail(404, 'Unknown brand kit')
      const tl = clone(c.timeline)
      tl.captions = { ...tl.captions, template_id: kit!.template_id, overrides: clone(kit!.overrides) as Timeline['captions']['overrides'] }
      tl.brand_kit_id = kit!.id
      tl.overlays = tl.overlays.filter((o) => o.kind === 'hook').map((o) => (o.kind === 'hook' ? { ...o, style: kit!.hook_style } : o))
      if (kit!.progress_bar.enabled) tl.overlays.push({ id: 'bar1', kind: 'progress_bar', color: kit!.progress_bar.color, position: 'bottom', thickness: 12 })
      if (kit!.logo_asset_id) tl.overlays.push({ id: 'logo1', kind: 'logo', asset_id: kit!.logo_asset_id, anchor: 'top_right', scale: 0.14, opacity: 0.9 })
      return commit(c, tl, 'studio_edit')
    })
  },
  async exportClip(id, body) {
    const c = clipOr404(id)
    await sleep(150)
    const first = c.timeline.segments[0].start
    return {
      job_id: makeJob('export', body.quality === 'preview' ? 3000 : 6500, {
        video_url: `${import.meta.env.BASE_URL}mock/source.webm#t=${first.toFixed(2)}`,
        srt_url: `data:text/plain;charset=utf-8,${encodeURIComponent('1\n00:00:00,000 --> 00:00:02,000\nMock caption export\n')}`,
        duration: duration(c.timeline),
      }, `Exporting ${c.summary.title}`),
    }
  },
  async getJob(id) {
    await sleep(50)
    const j = jobs.get(id)
    if (!j) return fail(404, 'Unknown job')
    const f = progressOf(j!.startedAt, j!.runMs)
    const info: JobInfo = {
      id,
      kind: j!.kind,
      status: j!.failed ? 'failed' : f >= 1 ? 'done' : f < 0.08 ? 'queued' : 'running',
      progress: f,
      error: j!.failed ?? null,
      result: f >= 1 ? j!.result : null,
    }
    return info
  },
  async templates() {
    await sleep(40)
    return clone(MOCK_REGISTRY)
  },
  async listBrandKits() {
    ensure()
    await sleep(60)
    return clone(brandKits)
  },
  async saveBrandKit(kit: BrandKitInput, id?: string) {
    ensure()
    await sleep(120)
    const logo_url = kit.logo_asset_id ? mockAssets.get(kit.logo_asset_id) ?? null : null
    if (id) {
      const i = brandKits.findIndex((k) => k.id === id)
      if (i < 0) fail(404, 'Unknown brand kit')
      brandKits[i] = { ...kit, id, logo_url }
      return clone(brandKits[i])
    }
    const created: BrandKit = { ...kit, id: uid('kit'), logo_url }
    brandKits.push(created)
    return clone(created)
  },
  async deleteBrandKit(id) {
    ensure()
    await sleep(80)
    const i = brandKits.findIndex((k) => k.id === id)
    if (i >= 0) brandKits.splice(i, 1)
  },
  async uploadBrandAsset(file) {
    await sleep(200)
    if (!/^image\/(png|jpeg)$/.test(file.type)) fail(422, 'Logos must be PNG or JPEG')
    if (file.size > 5 * 1024 * 1024) fail(422, 'Logos must be 5 MB or smaller')
    const url = await new Promise<string>((res, rej) => {
      const r = new FileReader()
      r.onload = () => res(String(r.result))
      r.onerror = rej
      r.readAsDataURL(file)
    })
    const asset_id = uid('asset')
    mockAssets.set(asset_id, url)
    return { asset_id, url }
  },
}
