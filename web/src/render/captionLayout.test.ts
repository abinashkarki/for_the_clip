import { describe, expect, it } from 'vitest'
import {
  animationProgress,
  animationState,
  buildPages,
  layout,
  pageIndexAt,
  resolveTemplate,
  wrapLines,
  wrapTextBlock,
} from './captionLayout'
import type { CaptionTemplate } from './types'

/** Deterministic stub: every character (and the space) is 0.5 em wide. */
const measure = (text: string, px: number) => text.length * px * 0.5

const TPL: CaptionTemplate = {
  id: 'bold_pop',
  name: 'Bold pop',
  font: { family: 'Montserrat', file: 'Montserrat-ExtraBold.ttf', size: 78, metrics: { ascent: 1, descent: 0.2 } },
  case: 'upper',
  grouping: { max_words: 3, max_chars: 18, max_lines: 2, break_on_pause: 0.35 },
  position: { y: 0.7, max_width: 0.86 },
  colors: { text: '#FFFFFF', active: '#FFE600', emphasis: '#3CF2FF', stroke: '#000000', box: null, active_box: null },
  stroke_width: 8,
  shadow: { dx: 0, dy: 6, blur: 0, color: '#00000099' },
  active_word: { style: 'color', scale: 1 },
  animation: { in: 'pop', duration_ms: 120 },
  line_height: 1.12,
}

function words(texts: string[], t0 = 0, dur = 0.3, gap = 0, first = 0) {
  let t = t0
  return texts.map((text, i) => {
    const w = { key: `s:${first + i}`, text, start: t, end: t + dur, emphasis: false, hidden: false }
    t += dur + gap
    return w
  })
}
const texts = (p: { lines: { words: { text: string }[] }[] }) => p.lines.flatMap((l) => l.words.map((w) => w.text))

describe('grouping', () => {
  it('starts a new page after max_words', () => {
    const pages = buildPages(TPL, words(['a', 'b', 'c', 'd', 'e']), 1080, 1920, measure)
    expect(pages.map((p) => p.words.length)).toEqual([3, 2])
  })
  it('respects max_chars on the cased text', () => {
    const tpl = { ...TPL, grouping: { ...TPL.grouping, max_words: 10, max_chars: 11 } }
    const pages = buildPages(tpl, words(['hello', 'world', 'again']), 1080, 1920, measure)
    expect(pages.map((p) => texts(p).join(' '))).toEqual(['HELLO WORLD', 'AGAIN'])
  })
  it('breaks on a pause >= break_on_pause', () => {
    const ws = [...words(['a', 'b'], 0, 0.3), ...words(['c', 'd'], 0.95, 0.3, 0, 2)]
    expect(buildPages(TPL, ws, 1080, 1920, measure).map((p) => p.words.length)).toEqual([2, 2])
  })
  it('never spans a range boundary', () => {
    const ws = words(['a', 'b', 'c'], 0, 0.3)
    const pages = buildPages(TPL, ws, 1080, 1920, measure, { boundaries: [0, 0.3] })
    expect(pages.map((p) => p.words.length)).toEqual([1, 2])
  })
  it('drops hidden and empty words and sorts by time', () => {
    const ws = [...words(['b', 'a']).reverse(), { key: 'x', text: ' ', start: 0.1, end: 0.2, emphasis: false, hidden: false },
      { key: 'h', text: 'hid', start: 0.1, end: 0.2, emphasis: false, hidden: true }]
    expect(texts(buildPages(TPL, ws, 1080, 1920, measure)[0])).toEqual(['B', 'A'])
  })
})

describe('wrapping', () => {
  it('wraps greedily against max_width', () => {
    // 10 px/char at size 20, space 10 px
    expect(wrapLines(['aaaa', 'bbbb', 'cccc'], measure, 20, 90)).toEqual([[0, 1], [2]])
    expect(wrapLines(['aaaa', 'bbbb', 'cccc'], measure, 20, 100)).toEqual([[0, 1], [2]])
    expect(wrapLines(['aa', 'bb', 'cc'], measure, 20, 140)).toEqual([[0, 1, 2]])
  })
  it('wraps hook text', () => {
    expect(wrapTextBlock('aaaa bbbb cccc', measure, 20, 90)).toEqual(['aaaa bbbb', 'cccc'])
    expect(wrapTextBlock('', measure, 20, 90)).toEqual([''])
  })
  it('shrinks a page whose single word is wider than max_width', () => {
    const p = buildPages(TPL, words(['abcdefghijklmnopqrstuabcdefghijklmnopqrstu']), 1080, 1920, measure)[0]
    const maxW = 0.86 * 1080
    expect(p.font_size).toBeLessThan(78)
    expect(p.lines[0].w).toBeLessThanOrEqual(maxW + 1e-6)
  })
  it('splits a group over max_lines and re-queues the rest', () => {
    const tpl = { ...TPL, grouping: { ...TPL.grouping, max_words: 6, max_chars: 99, max_lines: 1 }, font: { ...TPL.font, size: 150 } }
    const pages = buildPages(tpl, words(['aaaaa', 'bbbbb', 'ccccc', 'ddddd']), 1080, 1920, measure)
    expect(pages.length).toBeGreaterThan(1)
    expect(pages.every((p) => p.lines.length === 1)).toBe(true)
    expect(pages.flatMap((p) => texts(p))).toEqual(['AAAAA', 'BBBBB', 'CCCCC', 'DDDDD'])
  })
})

describe('geometry', () => {
  it('centres lines and places the baseline from font metrics', () => {
    const p = buildPages(TPL, words(['ab', 'cd']), 1080, 1920, measure)[0]
    const size = 78
    const line = p.lines[0]
    const lineH = size * 1.12
    expect(p.line_height_px).toBeCloseTo(lineH)
    expect(line.w).toBeCloseTo(4 * size * 0.5 + size * 0.5 + 0) // 2 words of 2 chars + a space
    expect(line.x).toBeCloseTo((1080 - line.w) / 2)
    expect(line.y).toBeCloseTo(0.7 * 1920 - lineH / 2)
    expect(line.baseline).toBeCloseTo(line.y + (lineH - 1.2 * size) / 2 + 1.0 * size)
    expect(line.words[1].x).toBeCloseTo(line.words[0].x + line.words[0].w + size * 0.5)
  })
  it('clamps the block inside the canvas', () => {
    const p = buildPages({ ...TPL, position: { y: 0.999, max_width: 0.86 } }, words(['ab']), 1080, 1920, measure)[0]
    expect(p.block.y + p.block.h).toBeLessThanOrEqual(1920 + 1e-9)
  })
  it('scales with the canvas width', () => {
    const a = buildPages(TPL, words(['ab']), 1080, 1920, measure)[0]
    const b = buildPages(TPL, words(['ab']), 540, 960, measure)[0]
    expect(b.font_size).toBeCloseTo(a.font_size / 2)
    expect(b.block.y).toBeCloseTo(a.block.y / 2)
  })
})

describe('visibility and active word', () => {
  const ws = words(['a', 'b', 'c', 'd'], 1, 0.3)
  it('shows a page from its first word until last_end + 0.6 or the next page', () => {
    const pages = buildPages({ ...TPL, grouping: { ...TPL.grouping, max_words: 2 } }, ws, 1080, 1920, measure)
    expect(pages[0].visible_end).toBeCloseTo(1.6) // next starts at 1.6
    expect(pages[1].visible_end).toBeCloseTo(2.2 + 0.6)
    expect(pageIndexAt(pages, 0.99)).toBe(-1)
    expect(pageIndexAt(pages, 1)).toBe(0)
    expect(pageIndexAt(pages, 1.6)).toBe(1)
    expect(pageIndexAt(pages, 2.8)).toBe(-1)
  })
  it('keeps the last spoken word active through gaps', () => {
    const l = layout(TPL, ws, 1080, 1920, 1.45, measure)!
    expect(l.active).toBe(1)
    expect(l.lines[0].words.map((w) => w.active)).toEqual([false, true, false])
  })
  it('first word is active at page start; none when style is none', () => {
    expect(layout(TPL, ws, 1080, 1920, 1, measure)!.active).toBe(0)
    const none = { ...TPL, active_word: { style: 'none' as const, scale: 1 } }
    expect(layout(none, ws, 1080, 1920, 1, measure)!.active).toBe(-1)
  })
  it('scales only the active word', () => {
    const tpl = { ...TPL, active_word: { style: 'scale' as const, scale: 1.2 } }
    const l = layout(tpl, ws, 1080, 1920, 1.35, measure)!
    expect(l.lines[0].words.map((w) => w.scale)).toEqual([1, 1.2, 1])
  })
})

describe('animation', () => {
  it('progress is 1 for none / zero duration, else clamped', () => {
    expect(animationProgress('none', 100, 0)).toBe(1)
    expect(animationProgress('pop', 0, 0)).toBe(1)
    expect(animationProgress('pop', 200, 0.1)).toBeCloseTo(0.5)
    expect(animationProgress('pop', 200, 9)).toBe(1)
    expect(animationProgress('pop', 200, -1)).toBe(0)
  })
  it('pop, fade, slide_up', () => {
    expect(animationState('pop', 0, 1)).toEqual({ scale: 0.85, alpha: 1, dy: 0 })
    expect(animationState('pop', 1, 1).scale).toBeCloseTo(1)
    expect(animationState('fade', 0.5, 1).alpha).toBeCloseTo(1 - 0.125)
    expect(animationState('slide_up', 0, 0.5).dy).toBeCloseTo(12)
    expect(animationState('slide_up', 1, 0.5).dy).toBeCloseTo(0)
  })
})

describe('resolveTemplate', () => {
  it('applies override-able keys and drops metrics on a font swap', () => {
    const r = resolveTemplate(TPL, {
      font: { family: 'Anton', file: 'Anton-Regular.ttf', size: 100 },
      colors: { active: '#00FF00' },
      position: { y: 0.5 },
      grouping: { max_words: 2 },
    })
    expect(r.font.file).toBe('Anton-Regular.ttf')
    expect(r.font.size).toBe(100)
    expect(r.font.metrics).toBeUndefined()
    expect(r.colors.active).toBe('#00FF00')
    expect(r.colors.text).toBe('#FFFFFF')
    expect(r.position.y).toBe(0.5)
    expect(r.grouping.max_words).toBe(2)
    expect(TPL.font.metrics).toBeDefined()
  })
  it('ignores a half font override', () => {
    expect(resolveTemplate(TPL, { font: { family: 'Anton' } }).font.family).toBe('Montserrat')
  })
})
