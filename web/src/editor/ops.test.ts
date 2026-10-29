import { describe, expect, it } from 'vitest'
import { baseTimeline } from '../render/fixtures'
import { duration, programMap } from '../render/timeline'
import {
  addCut,
  mergeCuts,
  moveShotBoundary,
  restoreRange,
  setHook,
  setLogo,
  setOverride,
  setProgressBar,
  setRegionFraming,
  setShotLayout,
  splitShot,
  toggleEmphasis,
} from './ops'

describe('cuts', () => {
  it('adds an interior cut inside a segment', () => {
    const tl = addCut(baseTimeline(), 'a', 16, 17)
    expect(tl.segments[0].cuts).toEqual([
      { start: 14, end: 15, reason: 'filler' },
      { start: 16, end: 17, reason: 'manual' },
    ])
    expect(duration(tl)).toBe(13)
  })
  it('merges overlapping cuts', () => {
    const tl = addCut(baseTimeline(), 'a', 14.5, 16)
    expect(tl.segments[0].cuts).toEqual([{ start: 14, end: 16, reason: 'manual' }])
  })
  it('trims the segment when the cut touches its edge', () => {
    const tl = addCut(baseTimeline(), 'a', 9, 11)
    expect(tl.segments[0]).toMatchObject({ start: 11, end: 20 })
    const tl2 = addCut(baseTimeline(), 'a', 19, 21)
    expect(tl2.segments[0].end).toBe(19)
  })
  it('removes a segment that is entirely cut, but never the last one', () => {
    const tl = addCut(baseTimeline(), 'a', 29, 36)
    expect(tl.segments.map((s) => s.id)).toEqual(['s1'])
    expect(addCut(tl, 'a', 0, 100)).toBe(tl)
  })
  it('ignores slivers and other sources', () => {
    const tl = baseTimeline()
    expect(addCut(tl, 'a', 16, 16.02)).toBe(tl)
    expect(addCut(tl, 'zzz', 16, 17).segments).toEqual(tl.segments)
  })
  it('stays valid per spec: sorted, non-overlapping, strictly inside', () => {
    let tl = baseTimeline()
    for (const [s, e] of [[18, 19], [11, 12], [12, 13], [13.9, 15.1], [10, 10.5]]) tl = addCut(tl, 'a', s, e)
    for (const seg of tl.segments) {
      let prev = seg.start
      for (const c of seg.cuts) {
        expect(c.start).toBeGreaterThan(prev - 1e-9)
        expect(c.end - c.start).toBeGreaterThanOrEqual(0.04 - 1e-9)
        expect(c.end).toBeLessThan(seg.end)
        prev = c.end
      }
    }
    expect(programMap(tl).length).toBeGreaterThan(0)
  })
  it('mergeCuts drops slivers', () => {
    expect(mergeCuts([{ start: 1, end: 1.01, reason: 'manual' }])).toEqual([])
  })
})

describe('restore', () => {
  it('removes a cut fully or partially', () => {
    const full = restoreRange(baseTimeline(), 'a', 14, 15)
    expect(full.segments[0].cuts).toEqual([])
    const part = restoreRange(baseTimeline(), 'a', 14.5, 15)
    expect(part.segments[0].cuts).toEqual([{ start: 14, end: 14.5, reason: 'filler' }])
    const mid = restoreRange(addCut(baseTimeline(), 'a', 16, 18), 'a', 16.8, 17.2)
    expect(mid.segments[0].cuts.map((c) => [c.start, c.end])).toEqual([[14, 15], [16, 16.8], [17.2, 18]])
  })
  it('extends a trimmed segment edge back and re-cuts the gap', () => {
    const trimmed = addCut(baseTimeline(), 'a', 9, 12) // segment now starts at 12
    const back = restoreRange(trimmed, 'a', 10, 10.5)
    expect(back.segments[0].start).toBe(10)
    expect(back.segments[0].cuts[0]).toMatchObject({ start: 10.5, end: 12 })
    expect(duration(back)).toBeCloseTo(duration(trimmed) + 0.5)
  })
  it('extends the nearer neighbour segment over a gap', () => {
    const tl = baseTimeline()
    const trail = restoreRange(tl, 'a', 22, 23) // s1 is nearer (gap 2 vs 7)
    expect(trail.segments[0].end).toBe(23)
    expect(trail.segments[0].cuts.at(-1)).toMatchObject({ start: 20, end: 22 })
    const lead = restoreRange(tl, 'a', 27, 28) // s2 is nearer (gap 2 vs 7)
    expect(lead.segments[1]).toMatchObject({ start: 27 })
    expect(lead.segments[1].cuts[0]).toMatchObject({ start: 28, end: 30 })
  })
})

describe('shots and framing', () => {
  it('changing layout seeds sensible regions', () => {
    let tl = setShotLayout(baseTimeline(), 'default', 'stack')
    expect(tl.shots[0].regions).toHaveLength(2)
    tl = setShotLayout(tl, 'default', 'fill')
    expect(tl.shots[0].regions).toHaveLength(1)
    expect(tl.shots[0].end).toBeCloseTo(14)
  })
  it('splits a shot and interpolates the new keyframe', () => {
    let tl = setShotLayout(baseTimeline(), 'default', 'fill')
    tl = splitShot(tl, 6)
    expect(tl.shots.map((s) => [s.start, s.end])).toEqual([[0, 6], [6, 14]])
    expect(tl.shots[1].regions[0].keyframes[0].t).toBe(6)
    expect(splitShot(tl, 6.1)).toBe(tl) // too close to boundary
  })
  it('moves a boundary within limits', () => {
    let tl = splitShot(setShotLayout(baseTimeline(), 'default', 'fill'), 6)
    tl = moveShotBoundary(tl, 0, 100)
    expect(tl.shots[0].end).toBeCloseTo(13.7)
    expect(tl.shots[1].start).toBeCloseTo(13.7)
  })
  it('framing: single keyframe moves, multi-keyframe upserts', () => {
    let tl = setShotLayout(baseTimeline(), 'default', 'fill')
    tl = setRegionFraming(tl, 'default', 0, 3, { x: 0.8, zoom: 1.5 })
    expect(tl.shots[0].regions[0].keyframes).toEqual([{ t: 0, x: 0.8, y: 0.5, zoom: 1.5 }])
    tl.shots[0].regions[0].keyframes.push({ t: 10, x: 0.2, y: 0.5, zoom: 1 })
    const upsert = setRegionFraming(tl, 'default', 0, 5, { x: 0.3 })
    expect(upsert.shots[0].regions[0].keyframes.map((k) => k.t)).toEqual([0, 5, 10])
    const clamped = setRegionFraming(tl, 'default', 0, 0, { x: 5, zoom: 9 })
    expect(clamped.shots[0].regions[0].keyframes[0]).toMatchObject({ x: 1, zoom: 3 })
  })
})

describe('captions and overlays', () => {
  it('overrides by path, pruning empties', () => {
    let tl = setOverride(baseTimeline(), 'colors.active', '#ff0000')
    expect(tl.captions.overrides).toEqual({ colors: { active: '#ff0000' } })
    tl = setOverride(tl, 'colors.active', undefined)
    expect(tl.captions.overrides).toEqual({})
    expect(setOverride(baseTimeline(), 'font.size', 90).captions.overrides).toEqual({ font: { size: 90 } })
  })
  it('toggles emphasis', () => {
    const on = toggleEmphasis(baseTimeline(), 'a:3')
    expect(on.captions.emphasis).toEqual(['a:3'])
    expect(toggleEmphasis(on, 'a:3').captions.emphasis).toEqual([])
  })
  it('keeps a single clamped hook', () => {
    let tl = setHook(baseTimeline(), { text: 'Hello', end: 99 })
    expect(tl.overlays).toHaveLength(1)
    expect(tl.overlays[0]).toMatchObject({ kind: 'hook', text: 'Hello', end: 14 })
    tl = setHook(tl, { start: 5 })
    expect(tl.overlays[0]).toMatchObject({ start: 5 })
    expect(setHook(tl, null).overlays).toHaveLength(0)
  })
  it('progress bar and logo constraints', () => {
    let tl = setProgressBar(baseTimeline(), { thickness: 100 })
    expect(tl.overlays[0]).toMatchObject({ kind: 'progress_bar', thickness: 40 })
    tl = setLogo(tl, { asset_id: 'x', scale: 0.9, opacity: 0 })
    const logo = tl.overlays.find((o) => o.kind === 'logo')
    expect(logo).toMatchObject({ scale: 0.4, opacity: 0.1 })
    expect(setLogo(baseTimeline(), { scale: 0.2 }).overlays).toHaveLength(0)
  })
})

describe('keyframes', () => {
  it('addKeyframe holds the current framing and is idempotent nearby', async () => {
    const { addKeyframe } = await import('./ops')
    let tl = setShotLayout(baseTimeline(), 'default', 'fill')
    tl = setRegionFraming(tl, 'default', 0, 0, { x: 0.3 })
    tl = addKeyframe(tl, 'default', 0, 13.95)
    expect(tl.shots[0].regions[0].keyframes.map((k) => [k.t, k.x])).toEqual([[0, 0.3], [13.95, 0.3]])
    expect(addKeyframe(tl, 'default', 0, 13.96).shots[0].regions[0].keyframes).toHaveLength(2)
  })
})
