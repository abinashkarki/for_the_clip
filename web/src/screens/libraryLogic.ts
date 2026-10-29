// Pure helpers for the Library: lifecycle filters, sorting and day countdowns.
import type { ProjectSummary } from '../api/types'
import { statusKind } from '../state/hooks'

export type LibraryChip = 'all' | 'active' | 'ready' | 'freed'

const DAY = 86400_000

export function chipOf(p: ProjectSummary): Exclude<LibraryChip, 'all'> {
  if (p.lifecycle === 'source_freed') return 'freed'
  return statusKind(p.status) === 'ready' ? 'ready' : 'active'
}

export const isTrashed = (p: ProjectSummary) => p.lifecycle === 'trashed'

/** newest activity first; falls back to creation time */
export function sortByActivity(list: ProjectSummary[]): ProjectSummary[] {
  const t = (p: ProjectSummary) => new Date(p.last_activity_at || p.created_at).getTime() || 0
  return [...list].sort((a, b) => t(b) - t(a))
}

export function visibleProjects(all: ProjectSummary[], chip: LibraryChip): ProjectSummary[] {
  return sortByActivity(all.filter((p) => !isTrashed(p) && (chip === 'all' || chipOf(p) === chip)))
}

export function trashedProjects(all: ProjectSummary[]): ProjectSummary[] {
  return [...all.filter(isTrashed)].sort((a, b) => new Date(b.trashed_at ?? 0).getTime() - new Date(a.trashed_at ?? 0).getTime())
}

export function chipCounts(all: ProjectSummary[]): Record<LibraryChip | 'trash', number> {
  const out = { all: 0, active: 0, ready: 0, freed: 0, trash: 0 }
  for (const p of all) {
    if (isTrashed(p)) out.trash++
    else {
      out.all++
      out[chipOf(p)]++
    }
  }
  return out
}

/** Whole days until an ISO time, rounded up, never negative. null when there is no date. */
export function daysUntil(iso: string | null | undefined, now = Date.now()): number | null {
  if (!iso) return null
  const t = new Date(iso).getTime()
  if (!isFinite(t)) return null
  return Math.max(0, Math.ceil((t - now) / DAY))
}

const plural = (n: number) => `${n} day${n === 1 ? '' : 's'}`

export function sourceFreesLabel(p: ProjectSummary, now = Date.now()): string | null {
  if (p.lifecycle !== 'active') return null
  const d = daysUntil(p.source_expires_at, now)
  if (d == null) return null
  return d === 0 ? 'Source frees today' : `Source frees in ${plural(d)}`
}

export function deletesInLabel(p: ProjectSummary, now = Date.now()): string {
  const d = daysUntil(p.purge_at, now)
  if (d == null) return 'In the trash'
  return d === 0 ? 'Deletes today' : `Deletes in ${plural(d)}`
}

export function formatBytes(n: number): string {
  if (!isFinite(n) || n <= 0) return '0 MB'
  if (n >= 1e9) return `${(n / 1e9).toFixed(n >= 1e10 ? 0 : 1)} GB`
  if (n >= 1e6) return `${Math.round(n / 1e6)} MB`
  return `${Math.max(1, Math.round(n / 1e3))} KB`
}

/** bytes shown on a card: source + exports (working files are regenerable and not worth mentioning) */
export const cardBytes = (p: ProjectSummary) => p.size_bytes.source + p.size_bytes.exports
/** bytes a "Free up space" action would release */
export const reclaimBytes = (p: ProjectSummary) => p.size_bytes.source + p.size_bytes.working

/** a failed project with nothing in it can be deleted inline */
export const isEmptyFailure = (p: ProjectSummary) => p.lifecycle === 'active' && statusKind(p.status) === 'failed' && p.clip_count === 0

export const RETENTION_OPTIONS = [
  { value: '7', label: '7 days' },
  { value: '15', label: '15 days' },
  { value: '30', label: '30 days' },
  { value: '0', label: 'Never' },
]

const CHIP_KEY = 'clippie.library.chip'
export function loadChip(): LibraryChip {
  try {
    const v = localStorage.getItem(CHIP_KEY)
    if (v === 'all' || v === 'active' || v === 'ready' || v === 'freed') return v
  } catch {
    /* storage unavailable */
  }
  return 'all'
}
export function saveChip(c: LibraryChip) {
  try {
    localStorage.setItem(CHIP_KEY, c)
  } catch {
    /* storage unavailable */
  }
}
