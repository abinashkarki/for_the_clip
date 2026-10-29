// Caption/hook font loading. Fonts are served by the app at /static/fonts/<file>.
// In mock mode they come from bundled woff2 copies under /studio/mock/fonts/.
import type { Measure } from './captionLayout'
import type { FontMetrics } from './types'

let baseUrl = '/static/fonts/'
let mockFonts = false
const loading = new Map<string, Promise<void>>()
const loadedFaces = new Map<string, FontFace>()

export function configureFonts(opts: { baseUrl?: string; mock?: boolean }) {
  if (opts.baseUrl) baseUrl = opts.baseUrl.endsWith('/') ? opts.baseUrl : opts.baseUrl + '/'
  if (opts.mock != null) mockFonts = opts.mock
}

/** CSS font-family name registered for a font file. */
export function faceName(file: string): string {
  return `clippie-${file.replace(/\.[a-z0-9]+$/i, '')}`
}

function urlFor(file: string): string {
  if (mockFonts) return `${import.meta.env.BASE_URL}mock/fonts/${file.replace(/\.[a-z0-9]+$/i, '')}.woff2`
  return `${baseUrl}${file}`
}

export function loadFont(file: string): Promise<void> {
  const hit = loading.get(file)
  if (hit) return hit
  if (typeof FontFace === 'undefined' || typeof document === 'undefined') return Promise.resolve()
  const p = (async () => {
    try {
      const face = new FontFace(faceName(file), `url(${urlFor(file)})`)
      await face.load()
      document.fonts.add(face)
      loadedFaces.set(file, face)
      metricsCache.delete(file)
    } catch {
      /* fall back to the generic family */
    }
  })()
  loading.set(file, p)
  return p
}

export function isFontReady(file: string): boolean {
  // FontFaceSet.check can return true for an unregistered family because a
  // fallback can draw it. Only our loaded, registered face makes layout stable.
  const face = loadedFaces.get(file)
  return typeof document !== 'undefined' && !!face && face.status === 'loaded' && document.fonts.has(face)
}

export function fontCss(file: string, px: number): string {
  return `${px}px "${faceName(file)}", "Inter", system-ui, sans-serif`
}

let measureCtx: CanvasRenderingContext2D | null = null
export function canvasMeasure(file: string): Measure {
  if (!measureCtx) measureCtx = document.createElement('canvas').getContext('2d')
  const ctx = measureCtx!
  return (text, px) => {
    ctx.font = fontCss(file, px)
    return ctx.measureText(text).width
  }
}

const metricsCache = new Map<string, FontMetrics>()
/** Ascent/descent in em from the browser font (used when the server template carries no `font.metrics`). */
export function fontMetrics(file: string): FontMetrics {
  const hit = metricsCache.get(file)
  if (hit) return hit
  if (!measureCtx) measureCtx = document.createElement('canvas').getContext('2d')
  const ctx = measureCtx!
  ctx.font = fontCss(file, 1000)
  const m = ctx.measureText('Hg')
  const out = { ascent: (m.fontBoundingBoxAscent || 1000) / 1000, descent: (m.fontBoundingBoxDescent || 300) / 1000 }
  if (isFontReady(file)) metricsCache.set(file, out)
  return out
}
