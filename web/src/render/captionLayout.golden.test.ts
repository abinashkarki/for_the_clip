// Golden test against stream B's fixtures (clippie.caption_golden/1): the preview layout must match the export
// layout within 0.06 px for numbers and exactly for text, keys and indexes.
import { describe, expect, it } from 'vitest'
import { type Measure, layout } from './captionLayout'
import type { CaptionTemplate } from './types'

interface GoldenWord {
  index: number
  key: string
  text: string
  x: number
  w: number
  emphasis: boolean
  active: boolean
  scale: number
}
interface GoldenPage {
  font_size: number
  line_height_px: number
  lines: { text: string; x: number; y: number; w: number; baseline: number; words: GoldenWord[] }[]
  block: { x: number; y: number; w: number; h: number }
  words: { key: string; start: number; end: number }[]
  index: number
  start: number
  last_end: number
  visible_end: number
  active: number
  progress: number
  animation: string
}
interface Golden {
  format: string
  measure: { space_widths: Record<string, number> }
  template: CaptionTemplate
  boundaries: number[]
  words: { key: string; text: string; start: number; end: number; emphasis: boolean; hidden: boolean }[]
  runs: { canvas: { width: number; height: number }; cases: { t: number; layout: { page: GoldenPage | null } }[] }[]
}

const goldens = Object.entries(import.meta.glob('./golden/*.json', { eager: true, import: 'default' })) as [string, Golden][]
const TOL = 0.06

/**
 * Pillow advances are hinted (integers), so they do not scale linearly with size. The fixture only carries widths of the
 * words it laid out, keyed here by (text, size); anything else falls back to a linear em estimate, which can only affect
 * pages the fixture does not check.
 */
function tableMeasure(g: Golden): Measure {
  const exact = new Map<string, number>()
  const em = new Map<string, number>()
  const spaces = new Map(Object.entries(g.measure.space_widths))
  const key = (text: string, px: number) => `${text}@${px.toFixed(1)}`
  let spaceEm = 0.3
  for (const run of g.runs) {
    for (const c of run.cases) {
      const page = c.layout.page
      if (!page) continue
      for (const line of page.lines) {
        for (const w of line.words) {
          exact.set(key(w.text, page.font_size), w.w)
          em.set(w.text, w.w / page.font_size)
        }
        const naturalSpace = spaces.get(page.font_size.toFixed(1))
        if (naturalSpace != null) spaceEm = naturalSpace / page.font_size
      }
    }
  }
  return (text, px) => {
    if (text === ' ') return spaces.get(px.toFixed(1)) ?? spaceEm * px
    return exact.get(key(text, px)) ?? (em.get(text) ?? text.length * 0.6) * px
  }
}

/**
 * The fixture's `template.font.metrics` are rounded to 2 decimals while the server positions baselines with the full
 * values. Only (ascent - descent) / 2 moves a baseline, so recover it from the first laid-out line and check it agrees
 * with the rounded metrics to within 0.01 em.
 */
function recoverMetrics(g: Golden) {
  for (const run of g.runs) {
    for (const c of run.cases) {
      const p = c.layout.page
      if (!p) continue
      const line = p.lines[0]
      const half = (line.baseline - line.y - p.line_height_px / 2) / p.font_size
      const rounded = g.template.font.metrics!
      expect(Math.abs(half - (rounded.ascent - rounded.descent) / 2)).toBeLessThan(0.01)
      return { ascent: 2 * half + rounded.descent, descent: rounded.descent }
    }
  }
  throw new Error('fixture has no visible case')
}

const near = (a: number, b: number, what: string) => {
  if (Math.abs(a - b) > TOL) throw new Error(`${what}: ${a} != ${b}`)
}

describe('caption golden fixtures', () => {
  it('finds fixtures', () => {
    expect(goldens.length).toBeGreaterThan(0)
  })
  for (const [, g] of goldens) {
    it(`${g.template.id} matches the export layout`, () => {
      expect(g.format).toBe('clippie.caption_golden/1')
      const measure = tableMeasure(g)
      const metrics = recoverMetrics(g)
      let checked = 0
      for (const run of g.runs) {
        const { width: W, height: H } = run.canvas
        for (const c of run.cases) {
          const got = layout(g.template, g.words, W, H, c.t, measure, { boundaries: g.boundaries, metrics })
          const want = c.layout.page
          const label = `${g.template.id} ${W}x${H} t=${c.t}`
          if (!want) {
            expect(got, label).toBeNull()
            continue
          }
          expect(got, label).not.toBeNull()
          const p = got!
          expect(p.index, label).toBe(want.index)
          expect(p.active, label).toBe(want.active)
          expect(p.animation, label).toBe(want.animation)
          expect(p.words.map((w) => w.key), label).toEqual(want.words.map((w) => w.key))
          for (const k of ['font_size', 'line_height_px', 'start', 'last_end', 'visible_end'] as const) {
            near(p[k], want[k], `${label} ${k}`)
          }
          near(p.progress, want.progress, `${label} progress`)
          for (const k of ['x', 'y', 'w', 'h'] as const) near(p.block[k], want.block[k], `${label} block.${k}`)
          expect(p.lines.length, label).toBe(want.lines.length)
          p.lines.forEach((line, i) => {
            const wl = want.lines[i]
            expect(line.text, label).toBe(wl.text)
            for (const k of ['x', 'y', 'w', 'baseline'] as const) near(line[k], wl[k], `${label} line${i}.${k}`)
            expect(line.words.length, label).toBe(wl.words.length)
            line.words.forEach((w, j) => {
              const ww = wl.words[j]
              expect(w.text, label).toBe(ww.text)
              expect(w.key, label).toBe(ww.key)
              expect(w.index, label).toBe(ww.index)
              expect(w.emphasis, label).toBe(ww.emphasis)
              expect(w.active, label).toBe(ww.active)
              near(w.scale, ww.scale, `${label} scale`)
              near(w.x, ww.x, `${label} line${i}.word${j}.x`)
              near(w.w, ww.w, `${label} line${i}.word${j}.w`)
            })
          })
          checked++
        }
      }
      expect(checked).toBeGreaterThan(5)
    })
  }
})
