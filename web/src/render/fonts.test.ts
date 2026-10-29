import { afterEach, describe, expect, it, vi } from 'vitest'
import { CaptionEngine } from './captionLayout'
import type { CaptionTemplate } from './types'
import boldPop from '../api/templates/bold_pop.json'

afterEach(() => vi.unstubAllGlobals())

async function environment(fail = false) {
  vi.resetModules()
  const faces = new Set<unknown>()
  let finish!: () => void
  const gate = new Promise<void>(resolve => { finish = resolve })
  let count = 0
  class Face {
    status = 'unloaded'
    constructor() { count++ }
    async load() {
      this.status = 'loading'
      await gate
      if (fail) { this.status = 'error'; throw new Error('font unavailable') }
      this.status = 'loaded'
      return this
    }
  }
  const context = { font: '', measureText: (text: string) => ({
    width: text.length * (faces.size ? 20 : 10),
    fontBoundingBoxAscent: faces.size ? 950 : 1200,
    fontBoundingBoxDescent: faces.size ? 230 : 400,
  }) }
  vi.stubGlobal('FontFace', Face)
  vi.stubGlobal('document', {
    // Native check() may say true even when this named family has not been registered.
    fonts: { check: () => true, add: (face: unknown) => faces.add(face), has: (face: unknown) => faces.has(face) },
    createElement: () => ({ getContext: () => context }),
  })
  const fonts = await import('./fonts')
  return { fonts, finish, count: () => count }
}

describe('caption font readiness', () => {
  it('does not trust fallback readiness and rebuilds cached geometry when the actual face arrives', async () => {
    const { fonts, finish, count } = await environment()
    const file = boldPop.font.file
    expect(document.fonts.check('16px nonexistent-family')).toBe(true)
    expect(fonts.isFontReady(file)).toBe(false)
    expect(fonts.fontMetrics(file)).toEqual({ ascent: 1.2, descent: .4 })
    const engine = new CaptionEngine(), tpl = boldPop as CaptionTemplate
    const words = [{ key: 's:0', text: 'NEVER', start: 0, end: 1 }]
    const update = () => engine.update(tpl, words, 1080, 1920, fonts.canvasMeasure(file),
      `revision|${fonts.isFontReady(file)}`, { metrics: fonts.fontMetrics(file) })
    update()
    const widthBefore = engine.pageList[0].lines[0].words[0].w
    const pending = fonts.loadFont(file)
    expect(fonts.loadFont(file)).toBe(pending)
    expect(count()).toBe(1)
    expect(fonts.isFontReady(file)).toBe(false)
    finish(); await pending
    expect(fonts.isFontReady(file)).toBe(true)
    expect(fonts.fontMetrics(file)).toEqual({ ascent: .95, descent: .23 })
    update()
    expect(engine.pageList[0].lines[0].words[0].w).toBe(widthBefore * 2)
  })

  it('keeps failed font loads in fallback state instead of caching a false ready state', async () => {
    const { fonts, finish } = await environment(true)
    const pending = fonts.loadFont(boldPop.font.file)
    finish(); await pending
    expect(fonts.isFontReady(boldPop.font.file)).toBe(false)
    expect(fonts.fontMetrics(boldPop.font.file)).toEqual({ ascent: 1.2, descent: .4 })
  })
})
