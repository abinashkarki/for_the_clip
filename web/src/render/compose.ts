// Frame composition: picture -> captions -> hook -> logo -> progress bar.
import { drawCaptions } from './captionDraw'
import { CaptionEngine, resolveTemplate } from './captionLayout'
import { canvasMeasure, fontMetrics, isFontReady } from './fonts'
import { BlurBackdrop, type PictureInput, drawPicture, shotAt } from './layout'
import { drawHook, drawLogo, drawProgressBar, hookAt } from './overlays'
import { effectiveShots } from './timeline'
import type {
  CaptionTemplate,
  HookOverlay,
  LogoOverlay,
  ProgramWord,
  ProgressBarOverlay,
  Shot,
  TemplateRegistry,
  Timeline,
} from './types'

export interface ComposeInput {
  timeline: Timeline
  t: number
  duration: number
  picture: PictureInput | null
  registry: TemplateRegistry | null
  words: ProgramWord[]
  /** program times where a kept range starts (a caption page never spans one) */
  boundaries: number[]
  /** changes whenever `words` or the caption settings change */
  version: string
  logos: Map<string, HTMLImageElement>
  /** hide captions/overlays (clean frame) */
  bare?: boolean
}

export class Compositor {
  private blur = new BlurBackdrop()
  private captions = new CaptionEngine()
  private resolved = new Map<string, CaptionTemplate>()

  resolve(registry: TemplateRegistry | null, tl: Timeline): CaptionTemplate | null {
    const base = registry?.captions.find((c) => c.id === tl.captions.template_id)
    if (!base) return null
    const key = `${base.id}|${JSON.stringify(tl.captions.overrides)}`
    let r = this.resolved.get(key)
    if (!r) {
      r = resolveTemplate(base, tl.captions.overrides)
      if (this.resolved.size > 40) this.resolved.clear()
      this.resolved.set(key, r)
    }
    return r
  }

  draw(ctx: CanvasRenderingContext2D, W: number, H: number, inp: ComposeInput): void {
    const { timeline: tl, t } = inp
    const shots: Shot[] = effectiveShots(tl)
    const shot = shotAt(shots, t)
    drawPicture(ctx, inp.picture, shot, t, W, H, tl.canvas.background, this.blur)
    if (inp.bare) return

    const tpl = tl.captions.enabled ? this.resolve(inp.registry, tl) : null
    if (tpl) {
      const measure = canvasMeasure(tpl.font.file)
      const metrics = tpl.font.metrics ?? fontMetrics(tpl.font.file)
      this.captions.update(tpl, inp.words, W, H, measure, `${inp.version}|${tpl.font.file}|${isFontReady(tpl.font.file)}`, {
        boundaries: inp.boundaries,
        metrics,
      })
      const lay = this.captions.at(tpl, t)
      if (lay) drawCaptions(ctx, lay, tpl, W, metrics)
    }

    const hook = hookAt(tl.overlays.filter((o): o is HookOverlay => o.kind === 'hook'), t)
    if (hook) {
      const style = inp.registry?.hooks.find((h) => h.id === hook.style)
      if (style) drawHook(ctx, hook, style, W, H, t)
    }
    for (const o of tl.overlays) {
      if (o.kind === 'logo') {
        const img = inp.logos.get((o as LogoOverlay).asset_id)
        if (img && img.complete && img.naturalWidth) drawLogo(ctx, o as LogoOverlay, img, W, H)
      }
    }
    const bar = tl.overlays.find((o): o is ProgressBarOverlay => o.kind === 'progress_bar')
    if (bar) drawProgressBar(ctx, bar, W, H, inp.duration > 0 ? t / inp.duration : 0)
  }
}
