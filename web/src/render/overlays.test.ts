import { describe, expect, it } from 'vitest'
import { layoutHook, logoRect, progressRect } from './overlays'
import type { HookStyle } from './types'

const measure = (text: string, px: number) => text.length * px * 0.5
const base: HookStyle = {
  id: 'hook_box',
  font: { family: 'Inter', file: 'Inter-Bold.ttf', metrics: { ascent: 1, descent: 0.2 } },
  size: 60,
  case: 'none',
  colors: { text: '#111', box: '#fff', stroke: null },
  stroke_width: 0,
  shadow: { dx: 0, dy: 0, blur: 0, color: '#00000000' },
  position: { y: 0.16 },
  max_width: 0.88,
  padding: 30,
  radius: 20,
  line_height: 1.1,
  animation: { in: 'pop', duration_ms: 160 },
}

describe('hook layout', () => {
  it('boxes the text with padding and centres the box on position.y', () => {
    const l = layoutHook({ text: 'Short hook' }, base, 1080, 1920, measure)
    expect(l.rows).toHaveLength(1)
    expect(l.boxW).toBeCloseTo(l.rows[0].w + 60)
    expect(l.boxH).toBeCloseTo(l.lineH + 60)
    expect(l.top).toBeCloseTo(0.16 * 1920 - l.boxH / 2)
    expect(l.rows[0].x).toBeCloseTo((1080 - l.rows[0].w) / 2)
  })
  it('wraps and shrinks to at most 4 lines within the width', () => {
    const text = 'this is a very long hook line that keeps going and going and going until it must shrink a lot '.repeat(2)
    const l = layoutHook({ text }, base, 1080, 1920, measure)
    expect(l.rows.length).toBeLessThanOrEqual(4)
    expect(l.size).toBeLessThan(60)
    expect(l.size).toBeGreaterThanOrEqual(60 * 0.55 * 0.94 - 1e-9)
  })
  it('band hooks span the canvas and ignore side padding for the text width', () => {
    const l = layoutHook({ text: 'hi' }, { ...base, band: true }, 1080, 1920, measure)
    expect(l.boxW).toBe(1080)
  })
  it('text-only hooks have no padding', () => {
    const l = layoutHook({ text: 'hi' }, { ...base, colors: { ...base.colors, box: null }, padding: 0 }, 1080, 1920, measure)
    expect(l.boxH).toBeCloseTo(l.lineH)
  })
})

describe('progress bar and logo geometry', () => {
  it('rounds like the exporter', () => {
    expect(progressRect({ id: 'b', kind: 'progress_bar', color: '#fff', thickness: 8, position: 'bottom' }, 540, 960, 0.5)).toEqual({
      x: 0,
      y: 956,
      w: 270,
      h: 4,
    })
    const r = logoRect({ id: 'l', kind: 'logo', asset_id: 'a', anchor: 'top_right', scale: 0.2, opacity: 1 } as never, 200, 100, 1080, 1920)
    expect(r).toEqual({ x: 1080 - 43 - 216, y: 43, w: 216, h: 108 })
  })
})
