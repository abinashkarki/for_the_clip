// Pure timeline edits. Every function returns a new timeline; inputs are never mutated.
import { defaultRegions, interpolate } from '../render/layout'
import { duration, effectiveShots, normalizeTimeline } from '../render/timeline'
import type {
  CaptionOverrides,
  Cut,
  CutReason,
  HookOverlay,
  Keyframe,
  LayoutKind,
  LogoOverlay,
  ProgressBarOverlay,
  Segment,
  Shot,
  Timeline,
} from '../render/types'

export const MIN_CUT = 0.04
const TOL = 0.001
const clone = <T>(v: T): T => JSON.parse(JSON.stringify(v))
const r3 = (v: number) => Math.round(v * 1000) / 1000

export function uniqueId(prefix: string, taken: Iterable<string>): string {
  const set = new Set(taken)
  let n = set.size + 1
  while (set.has(`${prefix}${n}`)) n++
  return `${prefix}${n}`
}

/** Merge cut list: sort, union overlaps, drop slivers shorter than MIN_CUT. */
export function mergeCuts(cuts: Cut[]): Cut[] {
  const sorted = [...cuts].sort((a, b) => a.start - b.start)
  const out: Cut[] = []
  for (const c of sorted) {
    const last = out[out.length - 1]
    if (last && c.start <= last.end + TOL) {
      last.end = Math.max(last.end, c.end)
      if (c.reason === 'manual') last.reason = 'manual'
    } else out.push({ ...c })
  }
  return out.filter((c) => c.end - c.start >= MIN_CUT - 1e-9)
}

/** Snap edge cuts into the segment bounds; return null if nothing is left. */
function settleSegment(seg: Segment): Segment | null {
  let { start, end } = seg
  const cuts = mergeCuts(seg.cuts.filter((c) => c.end > start + TOL && c.start < end - TOL)).map((c) => ({
    ...c,
    start: r3(Math.max(c.start, start)),
    end: r3(Math.min(c.end, end)),
  }))
  while (cuts.length && cuts[0].start <= start + TOL) start = cuts.shift()!.end
  while (cuts.length && cuts[cuts.length - 1].end >= end - TOL) end = cuts.pop()!.start
  if (end - start < MIN_CUT) return null
  return { ...seg, start: r3(start), end: r3(end), cuts: cuts.filter((c) => c.start > start && c.end < end) }
}

/** Remove [start,end] of a source from the programme (adds cuts / trims segments). */
export function addCut(tl: Timeline, sourceId: string, start: number, end: number, reason: CutReason = 'manual'): Timeline {
  if (end - start < MIN_CUT) return tl
  const segments: Segment[] = []
  for (const seg of tl.segments) {
    if (seg.source_id !== sourceId || end <= seg.start || start >= seg.end) {
      segments.push(seg)
      continue
    }
    const s = settleSegment({ ...seg, cuts: [...seg.cuts, { start: Math.max(start, seg.start), end: Math.min(end, seg.end), reason }] })
    if (s) segments.push(s)
  }
  if (!segments.length) return tl
  return normalizeTimeline({ ...tl, segments })
}

/** Put [start,end] of a source back in (removes cuts, extends a trimmed segment edge if needed). */
export function restoreRange(tl: Timeline, sourceId: string, start: number, end: number): Timeline {
  let touched = false
  let segments = tl.segments.map((seg) => {
    if (seg.source_id !== sourceId) return seg
    const cuts: Cut[] = []
    for (const c of seg.cuts) {
      if (c.end <= start + TOL || c.start >= end - TOL) {
        cuts.push(c)
        continue
      }
      touched = true
      if (c.start < start - TOL) cuts.push({ ...c, end: start })
      if (c.end > end + TOL) cuts.push({ ...c, start: end })
    }
    return { ...seg, cuts: mergeCuts(cuts) }
  })
  const mid = (start + end) / 2
  const inside = segments.some((s) => s.source_id === sourceId && mid >= s.start && mid < s.end)
  if (!touched && !inside) {
    const same = segments.filter((s) => s.source_id === sourceId)
    const lead = same.filter((s) => s.start >= end - TOL).sort((a, b) => a.start - b.start)[0]
    const trail = same.filter((s) => s.end <= start + TOL).sort((a, b) => b.end - a.end)[0]
    const gapLead = lead ? lead.start - end : Infinity
    const gapTrail = trail ? start - trail.end : Infinity
    const clear = (a: number, b: number) =>
      !same.some((s) => s !== lead && s !== trail && s.start < b && s.end > a)
    if (lead && gapLead <= gapTrail && clear(start, lead.start)) {
      segments = segments.map((s) => {
        if (s !== lead) return s
        const cuts = gapLead >= MIN_CUT ? mergeCuts([...s.cuts, { start: end, end: s.start, reason: 'manual' }]) : s.cuts
        return { ...s, start: start, cuts }
      })
    } else if (trail && clear(trail.end, end)) {
      segments = segments.map((s) => {
        if (s !== trail) return s
        const cuts = gapTrail >= MIN_CUT ? mergeCuts([...s.cuts, { start: s.end, end: start, reason: 'manual' }]) : s.cuts
        return { ...s, end: end, cuts }
      })
    }
  }
  return normalizeTimeline({ ...tl, segments })
}

export function removeFillerCuts(tl: Timeline): Timeline {
  return normalizeTimeline({
    ...tl,
    segments: tl.segments.map((s) => ({ ...s, cuts: s.cuts.filter((c) => c.reason === 'manual') })),
  })
}

export function clearCuts(tl: Timeline, reason?: CutReason): Timeline {
  return normalizeTimeline({
    ...tl,
    segments: tl.segments.map((s) => ({ ...s, cuts: reason ? s.cuts.filter((c) => c.reason !== reason) : [] })),
  })
}

// ---- shots / layout
function ensureShots(tl: Timeline): Shot[] {
  return clone(effectiveShots(tl)).map((s) => ({ ...s, end: Math.min(s.end, duration(tl)) }))
}

export function shotIndexAt(shots: Shot[], t: number): number {
  const i = shots.findIndex((s) => t < s.end)
  return i < 0 ? shots.length - 1 : i
}

export function setShotLayout(tl: Timeline, shotId: string, layout: LayoutKind): Timeline {
  const shots = ensureShots(tl).map((s) => {
    if (s.id !== shotId || s.layout === layout) return s
    const prev = s.regions.map((r) => interpolate(r.keyframes, s.start))
    return { ...s, layout, regions: defaultRegions(layout, s.start, prev) }
  })
  return { ...tl, shots }
}

/** Split the shot under t in two (min 0.5 s each side). */
export function splitShot(tl: Timeline, t: number): Timeline {
  const shots = ensureShots(tl)
  const i = shotIndexAt(shots, t)
  const s = shots[i]
  if (!s || t - s.start < 0.5 || s.end - t < 0.5) return tl
  const at = r3(t)
  const second: Shot = {
    ...clone(s),
    id: uniqueId('sh', shots.map((x) => x.id)),
    start: at,
    regions: s.regions.map((r) => ({ keyframes: [{ ...interpolate(r.keyframes, at), t: at }] })),
  }
  const first: Shot = { ...s, end: at, regions: s.regions.map((r) => ({ keyframes: r.keyframes.filter((k) => k.t < at) })) }
  first.regions = first.regions.map((r, k) => (r.keyframes.length ? r : { keyframes: [{ ...s.regions[k].keyframes[0], t: s.start }] }))
  shots.splice(i, 1, first, second)
  return { ...tl, shots }
}

/** Remove the shot boundary at shot index i (merge i into i-1). */
export function mergeShotWithPrevious(tl: Timeline, shotId: string): Timeline {
  const shots = ensureShots(tl)
  const i = shots.findIndex((s) => s.id === shotId)
  if (i <= 0) return tl
  shots[i - 1] = { ...shots[i - 1], end: shots[i].end }
  shots.splice(i, 1)
  return { ...tl, shots: shots.length === 1 && shots[0].layout === 'fit' ? [] : shots }
}

export function moveShotBoundary(tl: Timeline, boundaryIndex: number, t: number): Timeline {
  const shots = ensureShots(tl)
  const a = shots[boundaryIndex]
  const b = shots[boundaryIndex + 1]
  if (!a || !b) return tl
  const at = r3(Math.min(Math.max(t, a.start + 0.3), b.end - 0.3))
  a.end = at
  b.start = at
  a.regions = a.regions.map((r) => ({ keyframes: r.keyframes.filter((k) => k.t < at).length ? r.keyframes.filter((k) => k.t < at) : [{ ...r.keyframes[0], t: a.start }] }))
  b.regions = b.regions.map((r) => ({ keyframes: r.keyframes.map((k, i) => (i === 0 ? { ...k, t: at } : k)).filter((k) => k.t >= at) }))
  return { ...tl, shots }
}

const clamp = (v: number, lo: number, hi: number) => Math.min(Math.max(v, lo), hi)

/**
 * Set framing of a region. A single-keyframe region is moved; a keyframed region gets a keyframe
 * upserted at programme time t.
 */
export function setRegionFraming(
  tl: Timeline,
  shotId: string,
  region: number,
  t: number,
  f: Partial<Pick<Keyframe, 'x' | 'y' | 'zoom'>>,
): Timeline {
  const shots = ensureShots(tl).map((s) => {
    if (s.id !== shotId) return s
    const regions = s.regions.map((r, i) => {
      if (i !== region) return r
      const cur = interpolate(r.keyframes, t)
      const next = {
        x: clamp(f.x ?? cur.x, 0, 1),
        y: clamp(f.y ?? cur.y, 0, 1),
        zoom: clamp(f.zoom ?? cur.zoom, 1, 3),
      }
      if (r.keyframes.length <= 1) return { keyframes: [{ t: r.keyframes[0]?.t ?? s.start, ...next }] }
      const kt = r3(clamp(t, s.start, s.end))
      const existing = r.keyframes.findIndex((k) => Math.abs(k.t - kt) < 0.05)
      const kfs = r.keyframes.map((k) => ({ ...k }))
      if (existing >= 0) kfs[existing] = { t: kfs[existing].t, ...next }
      else kfs.push({ t: kt, ...next })
      kfs.sort((a, b) => a.t - b.t)
      return { keyframes: kfs }
    })
    return { ...s, regions }
  })
  return { ...tl, shots }
}

/** Add a keyframe at programme time t holding the current framing (no-op if one is already near). */
export function addKeyframe(tl: Timeline, shotId: string, region: number, t: number): Timeline {
  const shots = ensureShots(tl).map((s) => {
    if (s.id !== shotId) return s
    const regions = s.regions.map((r, i) => {
      if (i !== region) return r
      const kt = r3(clamp(t, s.start, s.end))
      if (r.keyframes.some((k) => Math.abs(k.t - kt) < 0.05)) return r
      const kfs = [...r.keyframes.map((k) => ({ ...k })), { t: kt, ...interpolate(r.keyframes, kt) }].sort((a, b) => a.t - b.t)
      return { keyframes: kfs }
    })
    return { ...s, regions }
  })
  return { ...tl, shots }
}

export function clearKeyframes(tl: Timeline, shotId: string, t: number): Timeline {
  const shots = ensureShots(tl).map((s) =>
    s.id !== shotId
      ? s
      : { ...s, regions: s.regions.map((r) => ({ keyframes: [{ ...interpolate(r.keyframes, t), t: s.start }] })) },
  )
  return { ...tl, shots }
}

// ---- captions
export function setCaptionTemplate(tl: Timeline, templateId: string, keepOverrides = false): Timeline {
  return { ...tl, captions: { ...tl.captions, template_id: templateId, overrides: keepOverrides ? tl.captions.overrides : {} } }
}

export function setCaptionsEnabled(tl: Timeline, enabled: boolean): Timeline {
  return { ...tl, captions: { ...tl.captions, enabled } }
}

/** Set an override by dotted path, e.g. `colors.active`. Passing undefined removes it. */
export function setOverride(tl: Timeline, path: string, value: unknown): Timeline {
  const next: Record<string, unknown> = clone(tl.captions.overrides) as Record<string, unknown>
  const keys = path.split('.')
  let cur = next
  keys.slice(0, -1).forEach((k) => {
    if (typeof cur[k] !== 'object' || cur[k] === null) cur[k] = {}
    cur = cur[k] as Record<string, unknown>
  })
  const leaf = keys[keys.length - 1]
  if (value === undefined) delete cur[leaf]
  else cur[leaf] = value
  // prune empty containers
  for (const k of Object.keys(next)) {
    const v = next[k]
    if (v && typeof v === 'object' && !Object.keys(v as object).length) delete next[k]
  }
  return { ...tl, captions: { ...tl.captions, overrides: next as CaptionOverrides } }
}

export function setFontOverride(tl: Timeline, family: string, file: string): Timeline {
  const o = clone(tl.captions.overrides) as CaptionOverrides
  o.font = { ...(o.font ?? {}), family, file }
  return { ...tl, captions: { ...tl.captions, overrides: o } }
}

function toggleKey(list: string[], key: string): string[] {
  return list.includes(key) ? list.filter((k) => k !== key) : [...list, key]
}
export const toggleEmphasis = (tl: Timeline, key: string): Timeline => ({
  ...tl,
  captions: { ...tl.captions, emphasis: toggleKey(tl.captions.emphasis, key) },
})
export const toggleHiddenWord = (tl: Timeline, key: string): Timeline => ({
  ...tl,
  captions: { ...tl.captions, hidden: toggleKey(tl.captions.hidden, key) },
})

// ---- overlays
export function getHook(tl: Timeline): HookOverlay | null {
  return (tl.overlays.find((o) => o.kind === 'hook') as HookOverlay | undefined) ?? null
}

export function setHook(tl: Timeline, patch: Partial<HookOverlay> | null): Timeline {
  const rest = tl.overlays.filter((o) => o.kind !== 'hook')
  if (patch === null) return { ...tl, overlays: rest }
  const dur = duration(tl)
  const cur = getHook(tl)
  const merged: HookOverlay = {
    id: cur?.id ?? 'hook1',
    kind: 'hook',
    text: 'Your hook here',
    start: 0,
    end: Math.min(3, dur),
    style: 'hook_box',
    ...cur,
    ...patch,
  }
  merged.start = clamp(merged.start, 0, Math.max(0, dur - 0.2))
  merged.end = clamp(merged.end, merged.start + 0.2, dur)
  return { ...tl, overlays: [merged, ...rest] }
}

export function getProgressBar(tl: Timeline): ProgressBarOverlay | null {
  return (tl.overlays.find((o) => o.kind === 'progress_bar') as ProgressBarOverlay | undefined) ?? null
}
export function setProgressBar(tl: Timeline, patch: Partial<ProgressBarOverlay> | null): Timeline {
  const rest = tl.overlays.filter((o) => o.kind !== 'progress_bar')
  if (patch === null) return { ...tl, overlays: rest }
  const cur = getProgressBar(tl)
  const bar: ProgressBarOverlay = { id: cur?.id ?? 'bar1', kind: 'progress_bar', color: '#FFE600', position: 'bottom', thickness: 12, ...cur, ...patch }
  bar.thickness = clamp(Math.round(bar.thickness), 4, 40)
  return { ...tl, overlays: [...rest, bar] }
}

export function getLogo(tl: Timeline): LogoOverlay | null {
  return (tl.overlays.find((o) => o.kind === 'logo') as LogoOverlay | undefined) ?? null
}
export function setLogo(tl: Timeline, patch: Partial<LogoOverlay> | null): Timeline {
  const rest = tl.overlays.filter((o) => o.kind !== 'logo')
  if (patch === null) return { ...tl, overlays: rest }
  const cur = getLogo(tl)
  if (!cur && !patch.asset_id) return tl
  const logo: LogoOverlay = { id: cur?.id ?? 'logo1', kind: 'logo', asset_id: '', anchor: 'top_right', scale: 0.14, opacity: 0.9, ...cur, ...patch }
  logo.scale = clamp(logo.scale, 0.05, 0.4)
  logo.opacity = clamp(logo.opacity, 0.1, 1)
  return { ...tl, overlays: [...rest, logo] }
}

export function setBackground(tl: Timeline, kind: 'blur' | 'color', color?: string): Timeline {
  return { ...tl, canvas: { ...tl.canvas, background: { kind, color: color ?? tl.canvas.background.color } } }
}
