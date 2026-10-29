// Live-rendered thumbnails (caption templates, hook styles) using the same code paths as the preview.
import { useEffect, useRef } from 'react'
import { drawCaptions } from '../render/captionDraw'
import { buildPages, layoutPage, pageIndexAt } from '../render/captionLayout'
import { canvasMeasure, fontMetrics, loadFont } from '../render/fonts'
import { cropWindow } from '../render/layout'
import { drawHook } from '../render/overlays'
import type { CaptionTemplate, HookStyle, ProgramWord } from '../render/types'
import { useRuntime } from './runtime'

const W = 180
const H = 320

export const SAMPLE_WORDS = ['Make', 'every', 'word', 'count', 'today'].map((text, i): ProgramWord => ({
  key: `sample:${i}`,
  text,
  start: i * 0.4,
  end: i * 0.4 + 0.4,
  source_id: 'sample',
  word_id: i,
  emphasis: false,
  hidden: false,
  range: 0,
}))

function paintBackdrop(ctx: CanvasRenderingContext2D, video: HTMLVideoElement | null) {
  const g = ctx.createLinearGradient(0, 0, W, H)
  g.addColorStop(0, '#2a3350')
  g.addColorStop(1, '#4d3a63')
  ctx.fillStyle = g
  ctx.fillRect(0, 0, W, H)
  if (video && video.readyState >= 2 && video.videoWidth) {
    const win = cropWindow({ w: video.videoWidth, h: video.videoHeight }, W / H, { x: 0.3, y: 0.5, zoom: 1 })
    ctx.drawImage(video, win.x, win.y, win.w, win.h, 0, 0, W, H)
    ctx.fillStyle = 'rgba(0,0,0,0.18)'
    ctx.fillRect(0, 0, W, H)
  }
}

export function CaptionThumb({ template, words, anchorTime }: { template: CaptionTemplate; words: ProgramWord[]; anchorTime: number }) {
  const rt = useRuntime()
  const ref = useRef<HTMLCanvasElement>(null)
  const wordsRef = useRef(words)
  wordsRef.current = words
  const anchorRef = useRef(anchorTime)
  anchorRef.current = anchorTime

  useEffect(() => {
    let alive = true
    const paint = () => {
      const canvas = ref.current
      if (!canvas || !alive) return
      const ctx = canvas.getContext('2d')!
      const video = (rt?.engine.frameSource as unknown as HTMLVideoElement | null) ?? null
      paintBackdrop(ctx, video)
      const measure = canvasMeasure(template.font.file)
      const metrics = template.font.metrics ?? fontMetrics(template.font.file)
      const opts = { metrics }
      let ws = wordsRef.current.length >= 3 ? wordsRef.current : SAMPLE_WORDS
      let pages = buildPages(template, ws, W, H, measure, opts)
      let idx = pageIndexAt(pages, anchorRef.current)
      if (idx < 0) {
        // nearest page at or after the playhead, else the first
        idx = pages.findIndex((p) => p.start >= anchorRef.current)
        if (idx < 0) idx = 0
      }
      if (!pages[idx]) {
        ws = SAMPLE_WORDS
        pages = buildPages(template, ws, W, H, measure, opts)
        idx = 0
      }
      const page = pages[idx]
      const mid = page.words[Math.min(1, page.words.length - 1)]
      const t = (mid.start + mid.end) / 2
      const still = { ...template, animation: { ...template.animation, in: 'none' as const } }
      drawCaptions(ctx, layoutPage(still, page, t), still, W, metrics)
    }
    void loadFont(template.font.file).then(paint)
    paint()
    const off = rt?.bus.subscribe(() => !rt.engine.playing && paint())
    return () => {
      alive = false
      off?.()
    }
  }, [rt, template])

  return <canvas ref={ref} width={W} height={H} className="thumb-canvas" aria-hidden="true" />
}

export function HookThumb({ style, text }: { style: HookStyle; text: string }) {
  const rt = useRuntime()
  const ref = useRef<HTMLCanvasElement>(null)
  useEffect(() => {
    let alive = true
    const paint = () => {
      const canvas = ref.current
      if (!canvas || !alive) return
      const ctx = canvas.getContext('2d')!
      paintBackdrop(ctx, (rt?.engine.frameSource as unknown as HTMLVideoElement | null) ?? null)
      drawHook(ctx, { id: 'h', kind: 'hook', text: text || 'Your hook here', start: 0, end: 10, style: style.id }, style, W, H, 1)
    }
    void loadFont(style.font.file).then(paint)
    paint()
    const off = rt?.bus.subscribe(() => !rt.engine.playing && paint())
    return () => {
      alive = false
      off?.()
    }
  }, [rt, style, text])
  return <canvas ref={ref} width={W} height={H} className="thumb-canvas" aria-hidden="true" />
}
