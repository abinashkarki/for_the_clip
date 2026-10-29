// Caption paging, wrapping, visibility, active word and animation: pure, mirroring clippie/caption_templates.py
// (docs/v3/CAPTION_TEMPLATES.md). `measure(text, fontPx)` returns the advance width in px of `text` at `fontPx` in the
// template font, so the same module runs under a golden fixture (table measure) and in the browser (canvas measureText).
import type { AnimationIn, CaptionOverrides, CaptionTemplate, FontMetrics, ProgramWord } from './types'

export type Measure = (text: string, fontPx: number) => number

export const VISIBLE_TAIL = 0.6
export const SLIDE_PX = 24
export const POP_FROM = 0.85
const EPS = 1e-9
/** used only when neither the template nor the browser can supply metrics */
export const DEFAULT_METRICS: FontMetrics = { ascent: 1.0, descent: 0.3 }

/** Merge timeline overrides onto a template (override-able keys only; derived font metrics are dropped on a font swap). */
export function resolveTemplate(t: CaptionTemplate, o: CaptionOverrides | undefined): CaptionTemplate {
  const out: CaptionTemplate = JSON.parse(JSON.stringify(t))
  if (!o) return out
  if (o.font) {
    if (o.font.family && o.font.file) {
      out.font.family = o.font.family
      out.font.file = o.font.file
    }
    if (o.font.size != null) out.font.size = o.font.size
    delete out.font.metrics
  }
  if (o.case) out.case = o.case
  if (o.position?.y != null) out.position.y = o.position.y
  if (o.colors) Object.assign(out.colors, o.colors)
  if (o.stroke_width != null) out.stroke_width = o.stroke_width
  if (o.active_word?.style) out.active_word.style = o.active_word.style
  if (o.animation?.in) out.animation.in = o.animation.in
  if (o.grouping?.max_words != null) out.grouping.max_words = o.grouping.max_words
  return out
}

export function applyCase(text: string, mode: CaptionTemplate['case']): string {
  return mode === 'upper' ? text.toUpperCase() : mode === 'lower' ? text.toLowerCase() : text
}

// ---------------------------------------------------------------- types

export interface PlacedWord {
  /** index into page.words */
  index: number
  key: string
  text: string
  x: number
  w: number
  emphasis: boolean
}
export interface PlacedLine {
  text: string
  x: number
  /** line box top */
  y: number
  w: number
  baseline: number
  words: PlacedWord[]
}
export interface CaptionPage {
  index: number
  font_size: number
  line_height_px: number
  lines: PlacedLine[]
  block: { x: number; y: number; w: number; h: number }
  words: { key: string; start: number; end: number }[]
  start: number
  last_end: number
  visible_end: number
}
export interface LaidWord extends PlacedWord {
  active: boolean
  scale: number
}
export interface LaidLine extends Omit<PlacedLine, 'words'> {
  words: LaidWord[]
}
export interface LaidPage extends Omit<CaptionPage, 'lines'> {
  lines: LaidLine[]
  active: number
  progress: number
  animation: AnimationIn
}

export interface PageOptions {
  /** program times where a kept range starts; a page never spans one */
  boundaries?: readonly number[]
  /** ascent/descent in em (defaults to template.font.metrics, then DEFAULT_METRICS) */
  metrics?: FontMetrics
}

// ---------------------------------------------------------------- helpers

/** Python bisect.bisect_right */
export function bisectRight(arr: readonly number[], x: number): number {
  let lo = 0
  let hi = arr.length
  while (lo < hi) {
    const mid = (lo + hi) >> 1
    if (x < arr[mid]) hi = mid
    else lo = mid + 1
  }
  return lo
}

export const easeOut = (p: number) => 1 - Math.pow(1 - p, 3)

export interface AnimState {
  scale: number
  alpha: number
  /** px at canvas scale, positive = down (slide_up starts below) */
  dy: number
}
/** `k` = canvasW / 1080 */
export function animationState(kind: AnimationIn, progress: number, k: number): AnimState {
  const e = easeOut(progress)
  if (kind === 'pop') return { scale: POP_FROM + (1 - POP_FROM) * e, alpha: 1, dy: 0 }
  if (kind === 'fade') return { scale: 1, alpha: e, dy: 0 }
  if (kind === 'slide_up') return { scale: 1, alpha: 1, dy: SLIDE_PX * k * (1 - e) }
  return { scale: 1, alpha: 1, dy: 0 }
}
export function animationProgress(kind: AnimationIn, durationMs: number, since: number): number {
  const span = durationMs / 1000
  return kind === 'none' || span <= 0 ? 1 : Math.min(1, Math.max(0, since / span))
}

/** Greedy wrap: word indexes per line. */
export function wrapLines(texts: readonly string[], measure: Measure, size: number, maxWidth: number, space = measure(' ', size)): number[][] {
  const lines: number[][] = []
  let current: number[] = []
  let currentW = 0
  texts.forEach((text, index) => {
    const width = measure(text, size)
    let needed = width + (current.length ? space : 0)
    if (current.length && currentW + needed > maxWidth + 1e-6) {
      lines.push(current)
      current = []
      currentW = 0
      needed = width
    }
    current.push(index)
    currentW += needed
  })
  if (current.length) lines.push(current)
  return lines
}

/** Wrap text into lines (hooks). */
export function wrapTextBlock(text: string, measure: Measure, size: number, maxWidth: number): string[] {
  const tokens = text.split(/\s+/).filter(Boolean)
  const lines = wrapLines(tokens, measure, size, maxWidth).map((l) => l.map((i) => tokens[i]).join(' '))
  return lines.length ? lines : ['']
}

interface Item {
  key: string
  text: string
  start: number
  end: number
  emphasis: boolean
}

function group(items: Item[], g: CaptionTemplate['grouping'], boundaries: readonly number[]): Item[][] {
  const groups: Item[][] = []
  let page: Item[] = []
  let chars = 0
  for (const item of items) {
    if (page.length) {
      const prev = page[page.length - 1]
      const crosses = bisectRight(boundaries, item.start + EPS) > bisectRight(boundaries, prev.start + EPS)
      if (
        page.length + 1 > g.max_words ||
        chars + item.text.length > g.max_chars ||
        item.start - prev.end >= g.break_on_pause - EPS ||
        crosses
      ) {
        groups.push(page)
        page = []
        chars = 0
      }
    }
    page.push(item)
    chars += item.text.length
  }
  if (page.length) groups.push(page)
  return groups
}

function pageGeometry(
  grp: Item[],
  tpl: CaptionTemplate,
  W: number,
  H: number,
  measure: Measure,
  metrics: FontMetrics,
): { page: Omit<CaptionPage, 'index' | 'start' | 'last_end' | 'visible_end'>; rest: Item[] } {
  const k = W / 1080
  const base = tpl.font.size * k
  const maxW = tpl.position.max_width * W
  const widest = Math.max(...grp.map((i) => measure(i.text, base)))
  const size = widest > 0 ? base * Math.min(1, maxW / widest) : base
  // Outlines extend into the font's space advance. Keep a visible 0.12 em
  // between neighbouring outlined glyphs, in wrapping and placement alike.
  const space = Math.max(measure(' ', size), 2 * tpl.stroke_width * k + size * 0.12)
  let lines = wrapLines(
    grp.map((i) => i.text),
    measure,
    size,
    maxW,
    space,
  )
  let rest: Item[] = []
  const maxLines = tpl.grouping.max_lines
  if (lines.length > maxLines) {
    const keep = lines.slice(0, maxLines).reduce((n, l) => n + l.length, 0)
    rest = grp.slice(keep)
    grp = grp.slice(0, keep)
    lines = lines.slice(0, maxLines)
  }
  const lineH = size * tpl.line_height
  const blockH = lineH * lines.length
  const top = Math.min(Math.max(tpl.position.y * H - blockH / 2, 0), Math.max(0, H - blockH))
  const out: PlacedLine[] = lines.map((indexes, row) => {
    const widths = indexes.map((i) => measure(grp[i].text, size))
    const lineW = widths.reduce((a, b) => a + b, 0) + space * (indexes.length - 1)
    let x = (W - lineW) / 2
    const lineTop = top + row * lineH
    const baseline = lineTop + (lineH - (metrics.ascent + metrics.descent) * size) / 2 + metrics.ascent * size
    const words: PlacedWord[] = indexes.map((i, n) => {
      const w: PlacedWord = { index: i, key: grp[i].key, text: grp[i].text, x, w: widths[n], emphasis: grp[i].emphasis }
      x += widths[n] + space
      return w
    })
    return { text: words.map((w) => w.text).join(' '), x: (W - lineW) / 2, y: lineTop, w: lineW, baseline, words }
  })
  return {
    page: {
      font_size: size,
      line_height_px: lineH,
      lines: out,
      block: {
        x: Math.min(...out.map((l) => l.x)),
        y: top,
        w: Math.max(...out.map((l) => l.w)),
        h: blockH,
      },
      words: grp.map((i) => ({ key: i.key, start: i.start, end: i.end })),
    },
    rest,
  }
}

export type WordInput = Pick<ProgramWord, 'key' | 'text' | 'start' | 'end'> & { emphasis?: boolean; hidden?: boolean }

/** All caption pages with static geometry. */
export function buildPages(
  tpl: CaptionTemplate,
  words: readonly WordInput[],
  W: number,
  H: number,
  measure: Measure,
  opts: PageOptions = {},
): CaptionPage[] {
  const metrics = opts.metrics ?? tpl.font.metrics ?? DEFAULT_METRICS
  const boundaries = [...(opts.boundaries ?? [])].sort((a, b) => a - b)
  const items: Item[] = words
    .filter((w) => !w.hidden && String(w.text).trim())
    .slice()
    .sort((a, b) => a.start - b.start || a.end - b.end)
    .map((w) => ({
      key: w.key,
      text: applyCase(String(w.text), tpl.case),
      start: w.start,
      end: w.end,
      emphasis: !!w.emphasis,
    }))
  const queue = group(items, tpl.grouping, boundaries)
  const result: CaptionPage[] = []
  while (queue.length) {
    const grp = queue.shift()!
    const { page, rest } = pageGeometry(grp, tpl, W, H, measure, metrics)
    if (rest.length) queue.unshift(rest)
    result.push({
      ...page,
      index: result.length,
      start: page.words[0].start,
      last_end: Math.max(...page.words.map((w) => w.end)),
      visible_end: 0,
    })
  }
  result.forEach((p, i) => {
    const stop = p.last_end + VISIBLE_TAIL
    const next = result[i + 1]
    p.visible_end = next ? Math.min(stop, next.start) : stop
  })
  return result
}

/** Index of the page visible at t, or -1. */
export function pageIndexAt(pages: readonly CaptionPage[], t: number): number {
  const i = bisectRight(
    pages.map((p) => p.start),
    t + EPS,
  ) - 1
  if (i < 0 || t >= pages[i].visible_end - EPS) return -1
  return i
}

/** The laid-out page visible at t (null if none). */
export function layoutAt(tpl: CaptionTemplate, pages: readonly CaptionPage[], t: number): LaidPage | null {
  const i = pageIndexAt(pages, t)
  return i < 0 ? null : layoutPage(tpl, pages[i], t)
}

export function layoutPage(tpl: CaptionTemplate, page: CaptionPage, t: number): LaidPage {
  const style = tpl.active_word.style
  const starts = page.words.map((w) => w.start)
  const active = style !== 'none' ? Math.max(0, bisectRight(starts, t + EPS) - 1) : -1
  const kind = tpl.animation.in
  const progress = animationProgress(kind, tpl.animation.duration_ms, t - page.start)
  return {
    ...page,
    lines: page.lines.map((line) => ({
      ...line,
      words: line.words.map((w) => {
        const on = w.index === active
        return { ...w, active: on, scale: on ? tpl.active_word.scale : 1 }
      }),
    })),
    active,
    progress,
    animation: kind,
  }
}

/** Golden-friendly entry point. */
export function layout(
  tpl: CaptionTemplate,
  words: readonly WordInput[],
  W: number,
  H: number,
  t: number,
  measure: Measure,
  opts: PageOptions = {},
): LaidPage | null {
  return layoutAt(tpl, buildPages(tpl, words, W, H, measure, opts), t)
}

/** Caches pages between frames; rebuilds only when the template, words or canvas size change. */
export class CaptionEngine {
  private pages: CaptionPage[] = []
  private key = ''
  private tpl: CaptionTemplate | null = null

  update(
    tpl: CaptionTemplate,
    words: readonly WordInput[],
    W: number,
    H: number,
    measure: Measure,
    version: string,
    opts: PageOptions,
  ) {
    const key = `${version}|${W}x${H}|${opts.metrics?.ascent ?? ''}`
    if (key === this.key && tpl === this.tpl) return
    this.key = key
    this.tpl = tpl
    this.pages = buildPages(tpl, words, W, H, measure, opts)
  }

  at(tpl: CaptionTemplate, t: number): LaidPage | null {
    return layoutAt(tpl, this.pages, t)
  }

  get pageList(): readonly CaptionPage[] {
    return this.pages
  }
}
