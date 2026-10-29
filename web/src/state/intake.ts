// Text and selection helpers for source intake and the activity tray. Pure, so they are unit tested.
import type { ActivityItem, ActivityResponse, ClipRange, InspectResult, IntakeStage, SourceQuality, SourceSummary } from '../api/types'

/** '~6 min left'. Empty when unknown. Mirrors the server so numbers read the same everywhere. */
export function formatEta(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds) || seconds < 0) return ''
  if (seconds < 45) return `~${Math.max(5, Math.round(seconds / 5) * 5)} s left`
  const minutes = Math.round(seconds / 60)
  if (minutes < 60) return `~${Math.max(1, minutes)} min left`
  const h = Math.floor(minutes / 60)
  const m = minutes % 60
  return m ? `~${h} h ${String(m).padStart(2, '0')} min left` : `~${h} h left`
}

export function formatBytes(bytes: number | null | undefined): string {
  if (bytes == null || !Number.isFinite(bytes) || bytes <= 0) return ''
  if (bytes >= 1e9) return `${(bytes / 1e9).toFixed(2)} GB`
  if (bytes >= 1e6) return `${(bytes / 1e6).toFixed(bytes >= 1e8 ? 0 : 1)} MB`
  return `${Math.max(1, Math.round(bytes / 1e3))} KB`
}

/** Length of a video or a wait: '2 h 35 min', '12 min', '45 s'. */
export function formatDuration(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds) || seconds < 0) return ''
  const s = Math.round(seconds)
  if (s < 60) return `${s} s`
  const minutes = Math.round(s / 60)
  if (minutes < 60) return `${minutes} min`
  const h = Math.floor(minutes / 60)
  const m = minutes % 60
  return m ? `${h} h ${m} min` : `${h} h`
}

export function formatClock(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds))
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const sec = s % 60
  return h ? `${h}:${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}` : `${m}:${String(sec).padStart(2, '0')}`
}

/** '1:02:03', '12:30', '90', '1h2m3s' to seconds; null when it is not a time. */
export function parseClock(text: string): number | null {
  const t = text.trim().toLowerCase()
  if (!t) return null
  const units = t.match(/^(?:(\d+(?:\.\d+)?)h)?\s*(?:(\d+(?:\.\d+)?)m)?\s*(?:(\d+(?:\.\d+)?)s)?$/)
  if (units && (units[1] || units[2] || units[3])) return Number(units[1] ?? 0) * 3600 + Number(units[2] ?? 0) * 60 + Number(units[3] ?? 0)
  if (!/^\d+(?::\d{1,2}){0,2}(?:\.\d+)?$/.test(t)) return null
  return t.split(':').reduce((total, part) => total * 60 + Number(part), 0)
}

export function validateRange(start: number | null, end: number | null, duration: number | null): string | null {
  if (start == null || end == null) return 'Enter a start and an end time, like 12:30 and 45:00.'
  if (end <= start) return 'The end must come after the start.'
  if (duration != null && start >= duration) return `The video is only ${formatClock(duration)} long.`
  return null
}

export const clampRange = (r: ClipRange, duration: number | null): ClipRange => ({ start: r.start, end: duration != null ? Math.min(r.end, duration) : r.end })

export function rangeLabel(range: ClipRange | null | undefined): string {
  return range ? `${formatClock(range.start)} to ${formatClock(range.end)}` : ''
}

// -- stages ---------------------------------------------------------------------

const BUSY = new Set(['queued', 'ingesting', 'transcribing', 'selecting', 'exporting', 'running'])
export const isBusy = (status: string) => BUSY.has(status)

export const activeStages = (stages: IntakeStage[] | undefined) => (stages ?? []).filter((s) => s.status === 'active')

/** What a person should read on a card: the stage in the lead, the others running beside it, or why nothing runs. */
export interface Headline {
  lead: IntakeStage | null
  also: IntakeStage[]
  text: string
  detail: string | null
  fraction: number | null
  eta: string
  stalled: boolean
  reason: string | null
  waiting: boolean
}

export function headline(source: SourceSummary): Headline {
  const stages = source.stages ?? []
  const active = activeStages(stages)
  const lead = active[0] ?? null
  const also = active.slice(1)
  const stalled = active.find((s) => s.stalled) ?? null
  if (source.status === 'cancelled') return blank('Cancelled', source.message ?? null)
  if (source.status === 'failed') return blank('Stopped', source.error)
  if (!lead) {
    const waiting = isBusy(source.status)
    return { ...blank(waiting ? 'Waiting' : 'Ready', null), text: source.queue?.message ?? (waiting ? 'Waiting to start' : 'Ready'), waiting }
  }
  return {
    lead,
    also,
    text: lead.label,
    detail: lead.detail,
    fraction: lead.fraction,
    eta: formatEta(lead.eta_seconds),
    stalled: Boolean(stalled),
    reason: stalled?.stall_reason ?? null,
    waiting: false,
  }
}

function blank(text: string, detail: string | null): Headline {
  return { lead: null, also: [], text, detail, fraction: null, eta: '', stalled: false, reason: null, waiting: false }
}

export const stageGlyph = (status: IntakeStage['status']): 'done' | 'active' | 'pending' | 'skipped' | 'failed' => status

// -- pre-flight --------------------------------------------------------------------

export function inspectedResolution(info: InspectResult, quality: SourceQuality): string {
  const height = info.options.find((option) => option.id === quality)?.height
  if (!height) return 'Available resolution could not be determined from this link.'
  const cap = { standard: 1080, high: 2160, small: 480 }[quality]
  return `Inspection found ${height}p for this choice${height < cap ? ` (below the ${cap}p limit)` : ''}. The downloaded resolution may vary.`
}

export function estimateLines(info: InspectResult, quality: SourceQuality, range: ClipRange | null): string[] {
  const share = range && info.duration ? Math.min(1, (range.end - range.start) / info.duration) : 1
  const bytes = info.estimates.download_bytes[quality]
  const seconds = info.estimates.download_seconds[quality]
  const lines: string[] = []
  if (bytes) lines.push(`Estimated download: about ${formatBytes(bytes * share)}${seconds ? `, around ${formatDuration(seconds * share)}` : ''}. Actual size and time can vary.`)
  else lines.push('Download size is unavailable for this link; actual size and time can vary.')
  lines.push(bytes ? `Estimated free space needed: about ${formatBytes(bytes * 3)} for temporary download and processing files. This is an estimate based on the full video.` : 'Estimated free space needed: unknown because the download size is unavailable.')
  if (info.estimates.transcribe_seconds) lines.push(`Transcription should take about ${formatDuration(info.estimates.transcribe_seconds * share)} on this computer.`)
  return lines
}

// -- activity ------------------------------------------------------------------------

const STATE_ORDER: Record<ActivityItem['state'], number> = { running: 0, waiting: 1, attention: 2, done: 3 }

export function sortActivity(items: ActivityItem[]): ActivityItem[] {
  return [...items].sort((a, b) => STATE_ORDER[a.state] - STATE_ORDER[b.state]
    || (a.state === 'attention' ? Number(b.kind === 'publish') - Number(a.kind === 'publish') : 0))
}

export function badge(activity: ActivityResponse | null): { count: number; attention: boolean } {
  return { count: activity?.running ?? 0, attention: (activity?.attention ?? 0) > 0 }
}

export const needsPolling = (activity: ActivityResponse | null) => (activity?.running ?? 0) > 0
