// program_map / program_words / duration exactly per TIMELINE_V3.md,
// plus source <-> program time mapping and shot normalisation.
import type { Cut, ProgramRange, ProgramWord, Segment, Shot, SourceWord, Timeline } from './types'

export const EPS = 1e-6

export function keptRanges(seg: Segment): { start: number; end: number }[] {
  const out: { start: number; end: number }[] = []
  let cursor = seg.start
  for (const cut of seg.cuts) {
    if (cut.start > cursor + EPS) out.push({ start: cursor, end: cut.start })
    cursor = Math.max(cursor, cut.end)
  }
  if (seg.end > cursor + EPS) out.push({ start: cursor, end: seg.end })
  return out
}

export function programMap(tl: Pick<Timeline, 'segments'>): ProgramRange[] {
  const out: ProgramRange[] = []
  let at = 0
  for (const seg of tl.segments) {
    for (const r of keptRanges(seg)) {
      out.push({ source_id: seg.source_id, src_start: r.start, src_end: r.end, at, segment_id: seg.id })
      at += r.end - r.start
    }
  }
  return out
}

export function duration(tl: Pick<Timeline, 'segments'>): number {
  let total = 0
  for (const seg of tl.segments) for (const r of keptRanges(seg)) total += r.end - r.start
  return total
}

export function rangeDuration(r: ProgramRange): number {
  return r.src_end - r.src_start
}

/** Index of the range containing program time t (last range for t >= duration). */
export function rangeIndexAt(map: ProgramRange[], t: number): number {
  if (!map.length) return -1
  for (let i = 0; i < map.length; i++) {
    const r = map[i]
    if (t < r.at + rangeDuration(r) - EPS) return Math.max(i, 0)
  }
  return map.length - 1
}

export interface SourcePoint {
  index: number
  source_id: string
  time: number
}

export function programToSource(map: ProgramRange[], t: number): SourcePoint | null {
  const index = rangeIndexAt(map, t)
  if (index < 0) return null
  const r = map[index]
  const local = Math.min(Math.max(t - r.at, 0), rangeDuration(r))
  return { index, source_id: r.source_id, time: r.src_start + local }
}

/** Program time of a source time; null if it falls in a cut or outside every range. */
export function sourceToProgram(map: ProgramRange[], sourceId: string, srcT: number): number | null {
  for (const r of map) {
    if (r.source_id !== sourceId) continue
    if (srcT >= r.src_start - EPS && srcT <= r.src_end + EPS) return r.at + (srcT - r.src_start)
  }
  return null
}

/** program_words per spec: midpoint inside a kept range, times clipped to the range then mapped. */
export function programWords(
  tl: Pick<Timeline, 'segments' | 'captions'>,
  words: SourceWord[],
): ProgramWord[] {
  const map = programMap(tl)
  const emphasis = new Set(tl.captions?.emphasis ?? [])
  const hidden = new Set(tl.captions?.hidden ?? [])
  const bySource = new Map<string, SourceWord[]>()
  for (const w of words) {
    const list = bySource.get(w.source_id)
    if (list) list.push(w)
    else bySource.set(w.source_id, [w])
  }
  const out: ProgramWord[] = []
  map.forEach((r, index) => {
    const list = bySource.get(r.source_id) ?? []
    const inRange: ProgramWord[] = []
    for (const w of list) {
      const mid = (w.start + w.end) / 2
      if (mid < r.src_start || mid >= r.src_end) continue
      const s = Math.max(w.start, r.src_start)
      const e = Math.min(w.end, r.src_end)
      inRange.push({
        key: w.key,
        text: w.text,
        source_id: w.source_id,
        word_id: w.word_id,
        start: r.at + (s - r.src_start),
        end: r.at + (e - r.src_start),
        emphasis: emphasis.has(w.key),
        hidden: hidden.has(w.key),
        range: index,
      })
    }
    inRange.sort((a, b) => a.start - b.start)
    out.push(...inRange)
  })
  return out
}

/** Words whose midpoint is outside every kept range (shown struck-through in the transcript). */
export function isWordRemoved(map: ProgramRange[], w: SourceWord): boolean {
  const mid = (w.start + w.end) / 2
  return !map.some((r) => r.source_id === w.source_id && mid >= r.src_start && mid < r.src_end)
}

/** Canonical shot list: absent/empty means one `fit` shot over the programme. */
export function effectiveShots(tl: Pick<Timeline, 'segments' | 'shots'>): Shot[] {
  if (tl.shots && tl.shots.length) return tl.shots
  return [
    {
      id: 'default',
      start: 0,
      end: duration(tl),
      layout: 'fit',
      regions: [{ keyframes: [{ t: 0, x: 0.5, y: 0.5, zoom: 1 }] }],
    },
  ]
}

/** Keep shots/overlays valid after the programme length changes (e.g. cuts). */
export function normalizeTimeline(tl: Timeline): Timeline {
  const dur = duration(tl)
  let shots = tl.shots
  if (shots.length) {
    const kept = shots.filter((s, i) => i === 0 || s.start < dur - 0.04)
    shots = kept.map((s, i) => {
      const end = i === kept.length - 1 ? dur : Math.min(s.end, dur)
      const regions = s.regions.map((r) => {
        const kfs = r.keyframes.filter((k) => k.t <= end + EPS)
        return { keyframes: kfs.length ? kfs : [{ ...r.keyframes[0], t: s.start }] }
      })
      return { ...s, end, regions }
    })
    if (shots.length && shots[0].start !== 0) shots[0] = { ...shots[0], start: 0 }
  }
  const overlays = tl.overlays
    .map((o) => {
      if (o.kind !== 'hook') return o
      const end = Math.min(o.end, dur)
      return end - o.start > 0.1 ? { ...o, end } : null
    })
    .filter((o): o is NonNullable<typeof o> => o !== null)
  return { ...tl, shots, overlays }
}

export function cutDuration(cuts: Cut[]): number {
  return cuts.reduce((a, c) => a + (c.end - c.start), 0)
}

export function formatTime(t: number, frames = false): string {
  if (!isFinite(t) || t < 0) t = 0
  const m = Math.floor(t / 60)
  const s = Math.floor(t % 60)
  const base = `${m}:${String(s).padStart(2, '0')}`
  if (!frames) return base
  const cs = Math.floor((t % 1) * 100)
  return `${base}.${String(cs).padStart(2, '0')}`
}
