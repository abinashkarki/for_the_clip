// Canvas drawing of a laid-out caption page, mirroring clippie/compositor.py (_draw_page).
import { type LaidPage, animationState } from './captionLayout'
import { fontCss } from './fonts'
import type { CaptionTemplate, FontMetrics } from './types'

type Ctx = CanvasRenderingContext2D

export const BOX_PAD_X = 0.45
export const BOX_PAD_Y = 0.22
export const BOX_RADIUS = 0.34
export const ACTIVE_PAD_X = 0.2
export const ACTIVE_PAD_Y = 0.07
export const ACTIVE_RADIUS = 0.26
export const ACTIVE_BOX_LIFT = 0.04

export function roundRect(ctx: Ctx, x: number, y: number, w: number, h: number, r: number) {
  const rr = Math.max(0, Math.min(r, w / 2, h / 2))
  ctx.beginPath()
  ctx.moveTo(x + rr, y)
  ctx.arcTo(x + w, y, x + w, y + h, rr)
  ctx.arcTo(x + w, y + h, x, y + h, rr)
  ctx.arcTo(x, y + h, x, y, rr)
  ctx.arcTo(x, y, x + w, y, rr)
  ctx.closePath()
}

/** "#RRGGBB" or "#RRGGBBAA" to a CSS colour canvas understands. */
export function cssColor(c: string): string {
  if (/^#[0-9a-f]{8}$/i.test(c)) {
    const a = parseInt(c.slice(7, 9), 16) / 255
    return `rgba(${parseInt(c.slice(1, 3), 16)},${parseInt(c.slice(3, 5), 16)},${parseInt(c.slice(5, 7), 16)},${a.toFixed(3)})`
  }
  return c
}

export interface ShadowSpec {
  dx: number
  dy: number
  blur: number
  color: string
}
export function applyShadow(ctx: Ctx, sh: ShadowSpec | undefined, k: number) {
  if (!sh || /^#[0-9a-f]{6}00$/i.test(sh.color)) return
  ctx.shadowColor = cssColor(sh.color)
  ctx.shadowOffsetX = Math.round(sh.dx * k)
  ctx.shadowOffsetY = Math.round(sh.dy * k)
  ctx.shadowBlur = sh.blur * k
}
export function clearShadow(ctx: Ctx) {
  ctx.shadowColor = 'rgba(0,0,0,0)'
  ctx.shadowOffsetX = 0
  ctx.shadowOffsetY = 0
  ctx.shadowBlur = 0
}

/** Draw one laid-out caption page. Canvas width W corresponds to 1080 authored px. */
export function drawCaptions(ctx: Ctx, lay: LaidPage, tpl: CaptionTemplate, W: number, metrics: FontMetrics) {
  const k = W / 1080
  const size = lay.font_size
  const anim = animationState(lay.animation, lay.progress, k)
  const c = tpl.colors
  const block = lay.block
  ctx.save()
  ctx.globalAlpha *= anim.alpha
  const cx = block.x + block.w / 2
  const cy = block.y + block.h / 2
  ctx.translate(cx, cy + anim.dy)
  ctx.scale(anim.scale, anim.scale)
  ctx.translate(-cx, -cy)
  ctx.textAlign = 'left'
  ctx.textBaseline = 'alphabetic'
  ctx.lineJoin = 'round'
  ctx.miterLimit = 2

  if (c.box) {
    const px = size * BOX_PAD_X
    const py = size * BOX_PAD_Y
    ctx.fillStyle = cssColor(c.box)
    roundRect(ctx, block.x - px, block.y - py, block.w + px * 2, block.h + py * 2, size * BOX_RADIUS)
    ctx.fill()
  }

  const style = tpl.active_word.style
  const stroke = Math.round(tpl.stroke_width * k)
  const lineH = lay.line_height_px

  // active word boxes sit under all text
  for (const line of lay.lines) {
    for (const w of line.words) {
      if (!w.active || style === 'none' || (style !== 'box' && style !== 'color_box')) continue
      const lineCy = line.y + lineH / 2
      const x = w.x + w.w / 2 - (w.w * w.scale) / 2
      const baseline = lineCy + (line.baseline - lineCy) * w.scale
      const asc = metrics.ascent * size * w.scale
      const desc = metrics.descent * size * w.scale
      const ex = size * ACTIVE_PAD_X
      const ey = size * ACTIVE_PAD_Y
      const lift = size * ACTIVE_BOX_LIFT
      ctx.fillStyle = cssColor(c.active_box ?? c.active)
      roundRect(ctx, x - ex, baseline - asc - ey + lift, w.w * w.scale + ex * 2, asc + desc + ey * 2 - lift * 2, size * ACTIVE_RADIUS)
      ctx.fill()
    }
  }

  // Paint every outline before any fill. A later word's thick outline must
  // never erase the preceding word's letter edge (the export does this too).
  for (const pass of stroke > 0 ? ['stroke', 'fill'] : ['fill']) {
    for (const line of lay.lines) {
      const lineCy = line.y + lineH / 2
      for (const w of line.words) {
        const active = w.active && style !== 'none'
        const color = active && (style === 'color' || style === 'color_box') ? c.active : w.emphasis ? c.emphasis : c.text
        ctx.save()
        if (w.scale !== 1) {
          const mx = w.x + w.w / 2
          ctx.translate(mx, lineCy)
          ctx.scale(w.scale, w.scale)
          ctx.translate(-mx, -lineCy)
        }
        ctx.font = fontCss(tpl.font.file, size)
        if (pass === 'stroke') {
          ctx.lineWidth = stroke * 2
          ctx.strokeStyle = cssColor(c.stroke)
          applyShadow(ctx, tpl.shadow, k)
          ctx.strokeText(w.text, w.x, line.baseline)
          clearShadow(ctx)
        } else if (stroke === 0) {
          applyShadow(ctx, tpl.shadow, k)
        }
        if (pass === 'fill') {
          ctx.fillStyle = cssColor(color)
          ctx.fillText(w.text, w.x, line.baseline)
        }
        clearShadow(ctx)
        ctx.restore()
      }
    }
  }
  ctx.restore()
}
