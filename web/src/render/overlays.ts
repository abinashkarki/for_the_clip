// Hook, progress bar and logo overlays, mirroring clippie/compositor.py.
import { applyShadow, clearShadow, cssColor, roundRect } from './captionDraw'
import { DEFAULT_METRICS, type Measure, animationProgress, animationState, applyCase, wrapTextBlock } from './captionLayout'
import { canvasMeasure, fontCss, fontMetrics } from './fonts'
import type { FontMetrics, HookOverlay, HookStyle, LogoOverlay, ProgressBarOverlay } from './types'

type Ctx = CanvasRenderingContext2D

export const HOOK_MIN_SCALE = 0.55
export const HOOK_MAX_LINES = 4
export const LOGO_MARGIN = 0.04

export function hookAt(hooks: HookOverlay[], t: number): HookOverlay | null {
  return hooks.find((h) => t >= h.start - 1e-9 && t < h.end - 1e-9) ?? null
}

export interface HookRow {
  text: string
  w: number
  x: number
  baseline: number
}
export interface HookLayout {
  size: number
  rows: HookRow[]
  top: number
  boxW: number
  boxH: number
  lineH: number
}

export function layoutHook(
  hook: Pick<HookOverlay, 'text'>,
  style: HookStyle,
  W: number,
  H: number,
  measure: Measure,
  metrics: FontMetrics = style.font.metrics ?? DEFAULT_METRICS,
): HookLayout {
  const k = W / 1080
  const text = applyCase(hook.text, style.case)
  const band = !!style.band
  let maxW = style.max_width * W - (band ? 0 : 2 * style.padding * k)
  maxW = Math.min(maxW, W - 2 * style.padding * k)
  let size = style.size * k
  let lines = wrapTextBlock(text, measure, size, maxW)
  while (
    (lines.length > HOOK_MAX_LINES || Math.max(...lines.map((l) => measure(l, size))) > maxW + 1e-6) &&
    size > style.size * k * HOOK_MIN_SCALE
  ) {
    size *= 0.94
    lines = wrapTextBlock(text, measure, size, maxW)
  }
  const lineH = size * style.line_height
  const pad = style.padding * k
  const boxed = !!style.colors.box || band
  const textH = lineH * lines.length
  const boxH = textH + (boxed ? 2 * pad : 0)
  const top = Math.min(Math.max(style.position.y * H - boxH / 2, 0), Math.max(0, H - boxH))
  const widths = lines.map((l) => measure(l, size))
  const rows = lines.map((line, i) => {
    const lineTop = top + (boxed ? pad : 0) + i * lineH
    const baseline = lineTop + (lineH - (metrics.ascent + metrics.descent) * size) / 2 + metrics.ascent * size
    return { text: line, w: widths[i], x: (W - widths[i]) / 2, baseline }
  })
  return { size, rows, top, boxW: band ? W : Math.max(...widths) + 2 * pad, boxH, lineH }
}

export function drawHook(ctx: Ctx, hook: HookOverlay, style: HookStyle, W: number, H: number, t: number) {
  const k = W / 1080
  const metrics = style.font.metrics ?? fontMetrics(style.font.file)
  const lay = layoutHook(hook, style, W, H, canvasMeasure(style.font.file), metrics)
  const progress = animationProgress(style.animation.in, style.animation.duration_ms, t - hook.start)
  const anim = animationState(style.animation.in, progress, k)
  const left = (W - lay.boxW) / 2
  ctx.save()
  ctx.globalAlpha *= anim.alpha
  const cx = left + lay.boxW / 2
  const cy = lay.top + lay.boxH / 2
  ctx.translate(cx, cy + anim.dy)
  ctx.scale(anim.scale, anim.scale)
  ctx.translate(-cx, -cy)
  const hasBox = !!style.colors.box
  if (hasBox) {
    applyShadow(ctx, style.shadow, k)
    ctx.fillStyle = cssColor(style.colors.box!)
    roundRect(ctx, left, lay.top, lay.boxW, lay.boxH, style.radius * k)
    ctx.fill()
    clearShadow(ctx)
  }
  ctx.font = fontCss(style.font.file, lay.size)
  ctx.textAlign = 'left'
  ctx.textBaseline = 'alphabetic'
  ctx.lineJoin = 'round'
  ctx.miterLimit = 2
  const stroke = Math.round(style.stroke_width * k)
  for (const row of lay.rows) {
    if (stroke > 0) {
      ctx.lineWidth = stroke * 2
      ctx.strokeStyle = cssColor(style.colors.stroke ?? '#000000')
      if (!hasBox) applyShadow(ctx, style.shadow, k)
      ctx.strokeText(row.text, row.x, row.baseline)
      clearShadow(ctx)
    } else if (!hasBox) {
      applyShadow(ctx, style.shadow, k)
    }
    ctx.fillStyle = cssColor(style.colors.text)
    ctx.fillText(row.text, row.x, row.baseline)
    clearShadow(ctx)
  }
  ctx.restore()
}

export function progressRect(o: ProgressBarOverlay, W: number, H: number, progress: number) {
  const th = Math.max(1, Math.round(o.thickness * (W / 1080)))
  const w = Math.round(W * Math.min(Math.max(progress, 0), 1))
  return { x: 0, y: o.position === 'top' ? 0 : H - th, w, h: th }
}

export function drawProgressBar(ctx: Ctx, o: ProgressBarOverlay, W: number, H: number, progress: number) {
  const r = progressRect(o, W, H, progress)
  if (r.w <= 0) return
  ctx.fillStyle = cssColor(o.color)
  ctx.fillRect(r.x, r.y, r.w, r.h)
}

export function logoRect(o: LogoOverlay, imgW: number, imgH: number, W: number, H: number) {
  const w = Math.max(1, Math.round(o.scale * W))
  const h = Math.max(1, Math.round((imgH * w) / Math.max(imgW, 1)))
  const m = Math.round(LOGO_MARGIN * W)
  const x = o.anchor.endsWith('left') ? m : W - m - w
  const y = o.anchor.startsWith('top') ? m : H - m - h
  return { x, y, w, h }
}

export function drawLogo(ctx: Ctx, o: LogoOverlay, img: CanvasImageSource & { width: number; height: number }, W: number, H: number) {
  const r = logoRect(o, img.width, img.height, W, H)
  ctx.save()
  ctx.globalAlpha = o.opacity
  ctx.drawImage(img, r.x, r.y, r.w, r.h)
  ctx.restore()
}
