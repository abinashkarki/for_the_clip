import { describe, expect, it } from 'vitest'
import { drawCaptions } from './captionDraw'
import { buildPages, layoutAt } from './captionLayout'
import type { CaptionTemplate } from './types'
import boldPop from '../api/templates/bold_pop.json'

const template = boldPop as CaptionTemplate
const words = ['THEY', 'NEVER', 'AGREE'].map((text, i) => ({ key: `s:${i}`, text, start: i * .3, end: (i + 1) * .3 }))
const measure = (text: string, size: number) => text === ' ' ? size * .05 : text.length * size * .5

describe('outlined captions', () => {
  it('keeps visible space outside thick outlines and uses the same gap to wrap', () => {
    const page = buildPages(template, words, 1080, 1920, measure)[0]
    const line = page.lines[0]
    const gap = line.words[1].x - line.words[0].x - line.words[0].w
    expect(gap).toBeCloseTo(22 + .12 * page.font_size)
    expect(line.x + line.w / 2).toBeCloseTo(540)
    // Natural spaces would fit two 100 px words in 210 px; outlined gaps do not.
    const narrow = { ...template, position: { ...template.position, max_width: 210 / 1080 } }
    const wrapped = buildPages(narrow, words.slice(0, 2), 1080, 1920, text => text === ' ' ? 1 : 100)[0]
    expect(wrapped.lines).toHaveLength(2)
    expect(wrapped.lines.every(l => l.w <= 210)).toBe(true)
  })

  it('draws every outline before any fill so later strokes cannot erase earlier letters', () => {
    const page = layoutAt(template, buildPages(template, words, 1080, 1920, measure), .4)!
    const calls: string[] = []
    const ctx = {
      globalAlpha: 1, save() {}, restore() {}, translate() {}, scale() {},
      strokeText(text: string) { calls.push(`stroke:${text}`) },
      fillText(text: string) { calls.push(`fill:${text}:${this.fillStyle}`) }, fillStyle: '',
    }
    drawCaptions(ctx as unknown as CanvasRenderingContext2D, page, template, 1080, { ascent: 1, descent: .2 })
    expect(calls).toEqual(['stroke:THEY', 'stroke:NEVER', 'stroke:AGREE',
      'fill:THEY:#FFFFFF', 'fill:NEVER:#FFE600', 'fill:AGREE:#FFFFFF'])
  })
})
