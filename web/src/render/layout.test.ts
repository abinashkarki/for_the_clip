import { describe, expect, it } from 'vitest'
import {
  BlurBackdrop,
  containRect,
  cropWindow,
  drawPicture,
  interpolate,
  placeRegions,
  regionSlots,
  shotAt,
} from './layout'
import type { Shot } from './types'

const SRC = { w: 1280, h: 720 }

describe('interpolate', () => {
  const kfs = [
    { t: 0, x: 0.2, y: 0.4, zoom: 1 },
    { t: 10, x: 0.6, y: 0.8, zoom: 2 },
  ]
  it('holds before and after', () => {
    expect(interpolate(kfs, -1)).toEqual({ x: 0.2, y: 0.4, zoom: 1 })
    expect(interpolate(kfs, 12)).toEqual({ x: 0.6, y: 0.8, zoom: 2 })
  })
  it('is linear between keyframes', () => {
    const m = interpolate(kfs, 5)
    expect(m.x).toBeCloseTo(0.4)
    expect(m.y).toBeCloseTo(0.6)
    expect(m.zoom).toBeCloseTo(1.5)
  })
  it('single keyframe is constant', () => {
    expect(interpolate([kfs[1]], 3)).toEqual({ x: 0.6, y: 0.8, zoom: 2 })
  })
})

describe('cropWindow', () => {
  it('zoom 1 is the largest 9:16 window inside a 16:9 source', () => {
    const w = cropWindow(SRC, 9 / 16, { x: 0.5, y: 0.5, zoom: 1 })
    expect(w.h).toBe(720)
    expect(w.w).toBeCloseTo(405)
    expect(w.x).toBeCloseTo(437.5)
  })
  it('zoom shrinks the window and clamps inside the source', () => {
    const w = cropWindow(SRC, 9 / 16, { x: 0, y: 0, zoom: 2 })
    expect(w.w).toBeCloseTo(202.5)
    expect(w.h).toBeCloseTo(360)
    expect(w.x).toBe(0)
    expect(w.y).toBe(0)
    const r = cropWindow(SRC, 9 / 16, { x: 1, y: 1, zoom: 2 })
    expect(r.x + r.w).toBeCloseTo(1280)
    expect(r.y + r.h).toBeCloseTo(720)
  })
  it('uses full width for a source narrower than the aspect', () => {
    const w = cropWindow({ w: 400, h: 1000 }, 1, { x: 0.5, y: 0.5, zoom: 1 })
    expect(w.w).toBe(400)
    expect(w.h).toBe(400)
  })
  it('clamps zoom to [1,3]', () => {
    expect(cropWindow(SRC, 1, { x: 0.5, y: 0.5, zoom: 9 }).h).toBeCloseTo(240)
    expect(cropWindow(SRC, 1, { x: 0.5, y: 0.5, zoom: 0.2 }).h).toBe(720)
  })
})

describe('regions', () => {
  it('slots per layout (9:16)', () => {
    expect(regionSlots('fill', 1080, 1920)).toHaveLength(1)
    const stack = regionSlots('stack', 1080, 1920)
    expect(stack[1].rect).toEqual({ x: 0, y: 960, w: 1080, h: 960 })
    const ss = regionSlots('screen_speaker', 1080, 1920)
    expect(ss[0].rect.h).toBeCloseTo(1152)
    expect(ss[1].rect.y).toBeCloseTo(1152)
    expect(ss[1].rect.h).toBeCloseTo(768)
    expect(ss[0].mode).toBe('fit')
  })
  it('fit letterboxes the whole frame, centred', () => {
    const r = containRect(SRC, { x: 0, y: 0, w: 1080, h: 1920 })
    expect(r.w).toBe(1080)
    expect(r.h).toBeCloseTo(607.5)
    expect(r.y).toBeCloseTo((1920 - 607.5) / 2)
  })
  it('placeRegions samples the crop for fill and the full frame for fit', () => {
    const fill: Shot = { id: 'a', start: 0, end: 5, layout: 'fill', regions: [{ keyframes: [{ t: 0, x: 0.5, y: 0.5, zoom: 1 }] }] }
    const [p] = placeRegions(fill, SRC, 540, 960, 1)
    expect(p.source.w).toBeCloseTo(405)
    expect(p.dest).toEqual({ x: 0, y: 0, w: 540, h: 960 })
    const fit: Shot = { ...fill, layout: 'fit' }
    const [q] = placeRegions(fit, SRC, 540, 960, 1)
    expect(q.source).toEqual({ x: 0, y: 0, w: 1280, h: 720 })
    expect(q.dest.w).toBe(540)
  })
  it('fit with zoom is a centred push-in', () => {
    const fit: Shot = { id: 'a', start: 0, end: 5, layout: 'fit', regions: [{ keyframes: [{ t: 0, x: 0.1, y: 0.1, zoom: 2 }] }] }
    const [q] = placeRegions(fit, SRC, 540, 960, 1)
    expect(q.source.w).toBeCloseTo(640)
    expect(q.source.x).toBeCloseTo(320) // x/y ignored
  })
  it('stack crops each half with half-canvas aspect', () => {
    const stack: Shot = {
      id: 's', start: 0, end: 5, layout: 'stack',
      regions: [{ keyframes: [{ t: 0, x: 0.4, y: 0.5, zoom: 1 }] }, { keyframes: [{ t: 0, x: 0.6, y: 0.5, zoom: 1 }] }],
    }
    const [a, b] = placeRegions(stack, SRC, 1080, 1920, 0)
    expect(a.source.w / a.source.h).toBeCloseTo(1080 / 960)
    expect(a.source.x + a.source.w / 2).toBeCloseTo(0.4 * 1280, 0)
    expect(b.source.x + b.source.w / 2).toBeCloseTo(0.6 * 1280, 0)
    expect(b.dest.y).toBe(960)
  })
  it('shotAt picks by time with last as fallback', () => {
    const shots = [{ start: 0, end: 2 }, { start: 2, end: 4 }]
    expect(shotAt(shots, 1)).toBe(shots[0])
    expect(shotAt(shots, 2)).toBe(shots[1])
    expect(shotAt(shots, 99)).toBe(shots[1])
  })
})

describe('drawPicture', () => {
  function mockCtx() {
    const calls: string[] = []
    const ctx = {
      save: () => calls.push('save'),
      restore: () => calls.push('restore'),
      beginPath: () => {},
      rect: () => {},
      clip: () => calls.push('clip'),
      fillStyle: '',
      fillRect: (...a: number[]) => calls.push(`fillRect ${a.join(',')}`),
      drawImage: (_i: unknown, ...a: number[]) => calls.push(`drawImage ${a.length}`),
      globalAlpha: 1,
      imageSmoothingEnabled: true,
    }
    return { ctx, calls }
  }
  it('fill draws one clipped crop; stack draws two', () => {
    const { ctx, calls } = mockCtx()
    const fill: Shot = { id: 'a', start: 0, end: 5, layout: 'fill', regions: [{ keyframes: [{ t: 0, x: 0.5, y: 0.5, zoom: 1 }] }] }
    drawPicture(ctx, { img: {} as CanvasImageSource, src: SRC }, fill, 0, 540, 960, { kind: 'color', color: '#123' }, new BlurBackdrop())
    expect(calls.filter((c) => c.startsWith('drawImage'))).toHaveLength(1)
    const stack: Shot = { ...fill, layout: 'stack', regions: [fill.regions[0], fill.regions[0]] }
    calls.length = 0
    drawPicture(ctx, { img: {} as CanvasImageSource, src: SRC }, stack, 0, 540, 960, { kind: 'color', color: '#123' }, new BlurBackdrop())
    expect(calls.filter((c) => c.startsWith('drawImage'))).toHaveLength(2)
  })
  it('paints only the background when no frame is available', () => {
    const { ctx, calls } = mockCtx()
    const fill: Shot = { id: 'a', start: 0, end: 5, layout: 'fill', regions: [{ keyframes: [{ t: 0, x: 0.5, y: 0.5, zoom: 1 }] }] }
    drawPicture(ctx, null, fill, 0, 540, 960, { kind: 'color', color: '#123' }, new BlurBackdrop())
    expect(calls).toEqual(['fillRect 0,0,540,960'])
  })
})
