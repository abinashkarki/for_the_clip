// Shot lookup, keyframe interpolation, crop-window maths and canvas drawing of the four layouts.
import type { Keyframe, LayoutKind, Shot } from './types'

export interface Rect {
  x: number
  y: number
  w: number
  h: number
}
export interface Size {
  w: number
  h: number
}
export type Framing = { x: number; y: number; zoom: number }

const clamp = (v: number, lo: number, hi: number) => Math.min(Math.max(v, lo), hi)

export function shotAt<T extends { start: number; end: number }>(shots: T[], t: number): T {
  for (const s of shots) if (t < s.end) return s
  return shots[shots.length - 1]
}

export function interpolate(kfs: Keyframe[], t: number): Framing {
  if (!kfs.length) return { x: 0.5, y: 0.5, zoom: 1 }
  if (t <= kfs[0].t || kfs.length === 1) return { x: kfs[0].x, y: kfs[0].y, zoom: kfs[0].zoom }
  const last = kfs[kfs.length - 1]
  if (t >= last.t) return { x: last.x, y: last.y, zoom: last.zoom }
  for (let i = 1; i < kfs.length; i++) {
    const b = kfs[i]
    if (t <= b.t) {
      const a = kfs[i - 1]
      const k = (t - a.t) / (b.t - a.t || 1)
      return { x: a.x + (b.x - a.x) * k, y: a.y + (b.y - a.y) * k, zoom: a.zoom + (b.zoom - a.zoom) * k }
    }
  }
  return { x: last.x, y: last.y, zoom: last.zoom }
}

/**
 * Crop window in source pixels. zoom 1 = largest window of `aspect` (w/h) inside the source;
 * the window is clamped inside the source frame.
 */
export function cropWindow(src: Size, aspect: number, f: Framing): Rect {
  const srcAspect = src.w / src.h
  let w: number
  let h: number
  if (srcAspect > aspect) {
    h = src.h
    w = h * aspect
  } else {
    w = src.w
    h = w / aspect
  }
  const zoom = clamp(f.zoom, 1, 3)
  w /= zoom
  h /= zoom
  const x = clamp(f.x * src.w - w / 2, 0, src.w - w)
  const y = clamp(f.y * src.h - h / 2, 0, src.h - h)
  return { x, y, w, h }
}

/** Largest rect of src's aspect inside `rect`, centred (letterbox). */
export function containRect(src: Size, rect: Rect): Rect {
  const s = Math.min(rect.w / src.w, rect.h / src.h)
  const w = src.w * s
  const h = src.h * s
  return { x: rect.x + (rect.w - w) / 2, y: rect.y + (rect.h - h) / 2, w, h }
}

export type RegionMode = 'crop' | 'fit'
export interface RegionSlot {
  rect: Rect
  mode: RegionMode
}

export function regionSlots(layout: LayoutKind, W: number, H: number): RegionSlot[] {
  switch (layout) {
    case 'fill':
      return [{ rect: { x: 0, y: 0, w: W, h: H }, mode: 'crop' }]
    case 'fit':
      return [{ rect: { x: 0, y: 0, w: W, h: H }, mode: 'fit' }]
    case 'stack':
      return [
        { rect: { x: 0, y: 0, w: W, h: H / 2 }, mode: 'crop' },
        { rect: { x: 0, y: H / 2, w: W, h: H / 2 }, mode: 'crop' },
      ]
    case 'screen_speaker':
      return [
        { rect: { x: 0, y: 0, w: W, h: H * 0.6 }, mode: 'fit' },
        { rect: { x: 0, y: H * 0.6, w: W, h: H * 0.4 }, mode: 'crop' },
      ]
  }
}

export interface PlacedRegion {
  index: number
  mode: RegionMode
  /** destination rect on the canvas (the area the picture occupies) */
  dest: Rect
  /** slot rect on the canvas (clip area) */
  slot: Rect
  /** source rectangle sampled, in source pixels */
  source: Rect
  framing: Framing
}

export function framingAt(shot: Shot, region: number, t: number): Framing {
  const r = shot.regions[region] ?? shot.regions[0]
  return interpolate(r?.keyframes ?? [], t)
}

/** Resolve where each region of a shot lands on a W x H canvas at program time t. */
export function placeRegions(shot: Shot, src: Size, W: number, H: number, t: number): PlacedRegion[] {
  return regionSlots(shot.layout, W, H).map((slot, index) => {
    const framing = framingAt(shot, index, t)
    if (slot.mode === 'fit') {
      const dest = containRect(src, slot.rect)
      const source = cropWindow(src, src.w / src.h, { x: 0.5, y: 0.5, zoom: framing.zoom })
      return { index, mode: 'fit', dest, slot: slot.rect, source, framing }
    }
    const source = cropWindow(src, slot.rect.w / slot.rect.h, framing)
    return { index, mode: 'crop', dest: slot.rect, slot: slot.rect, source, framing }
  })
}

export interface DrawCtx {
  save(): void
  restore(): void
  beginPath(): void
  rect(x: number, y: number, w: number, h: number): void
  clip(): void
  fillStyle: string | CanvasGradient | CanvasPattern
  fillRect(x: number, y: number, w: number, h: number): void
  drawImage(img: CanvasImageSource, ...args: number[]): void
  globalAlpha: number
  imageSmoothingEnabled: boolean
  imageSmoothingQuality?: 'low' | 'medium' | 'high'
}

export interface BackgroundSpec {
  kind: 'blur' | 'color'
  color: string
}

/** Cheap blurred backdrop: downsample the cover-fitted frame to a tiny canvas and scale it back up. */
export class BlurBackdrop {
  private tiny: HTMLCanvasElement | OffscreenCanvas | null = null
  private tinyCtx: CanvasRenderingContext2D | OffscreenCanvasRenderingContext2D | null = null

  draw(ctx: DrawCtx, img: CanvasImageSource, src: Size, W: number, H: number, tint = 0.38) {
    const tw = 40
    const th = Math.max(8, Math.round((tw * H) / W))
    if (!this.tiny || this.tiny.width !== tw || this.tiny.height !== th) {
      this.tiny = typeof OffscreenCanvas !== 'undefined' ? new OffscreenCanvas(tw, th) : makeCanvas(tw, th)
      this.tinyCtx = this.tiny.getContext('2d') as CanvasRenderingContext2D
    }
    const win = cropWindow(src, W / H, { x: 0.5, y: 0.5, zoom: 1.2 })
    this.tinyCtx!.drawImage(img, win.x, win.y, win.w, win.h, 0, 0, tw, th)
    ctx.imageSmoothingEnabled = true
    ctx.imageSmoothingQuality = 'high'
    ctx.drawImage(this.tiny as CanvasImageSource, 0, 0, W, H)
    ctx.globalAlpha = tint
    ctx.fillStyle = '#000'
    ctx.fillRect(0, 0, W, H)
    ctx.globalAlpha = 1
  }
}

function makeCanvas(w: number, h: number): HTMLCanvasElement {
  const c = document.createElement('canvas')
  c.width = w
  c.height = h
  return c
}

export interface PictureInput {
  img: CanvasImageSource
  src: Size
}

/** Paint background + the picture for one shot onto a W x H canvas context. */
export function drawPicture(
  ctx: DrawCtx,
  pic: PictureInput | null,
  shot: Shot,
  t: number,
  W: number,
  H: number,
  bg: BackgroundSpec,
  blur: BlurBackdrop,
): PlacedRegion[] {
  ctx.globalAlpha = 1
  ctx.fillStyle = bg.kind === 'color' ? bg.color : '#000'
  ctx.fillRect(0, 0, W, H)
  if (!pic || pic.src.w <= 0 || pic.src.h <= 0) return []
  const placed = placeRegions(shot, pic.src, W, H, t)
  // Backdrop only matters where a region leaves part of the canvas uncovered.
  const covered = placed.every((p) => p.mode === 'crop')
  if (bg.kind === 'blur' && !covered) blur.draw(ctx, pic.img, pic.src, W, H)
  for (const p of placed) {
    ctx.save()
    ctx.beginPath()
    ctx.rect(p.slot.x, p.slot.y, p.slot.w, p.slot.h)
    ctx.clip()
    ctx.imageSmoothingEnabled = true
    ctx.drawImage(pic.img, p.source.x, p.source.y, p.source.w, p.source.h, p.dest.x, p.dest.y, p.dest.w, p.dest.h)
    ctx.restore()
  }
  return placed
}

/** Default region framing for a layout (used when switching layouts in the editor). */
export function defaultRegions(layout: LayoutKind, t: number, prev?: Framing[]): Shot['regions'] {
  const mk = (x: number, y: number, zoom = 1) => ({ keyframes: [{ t, x, y, zoom }] })
  const p0 = prev?.[0]
  switch (layout) {
    case 'fill':
      return [mk(p0?.x ?? 0.5, p0?.y ?? 0.5, p0?.zoom ?? 1)]
    case 'fit':
      return [mk(0.5, 0.5, 1)]
    case 'stack':
      return [mk(0.25, 0.45), mk(0.75, 0.45)]
    case 'screen_speaker':
      return [mk(0.5, 0.5, 1), mk(0.78, 0.45, 1.4)]
  }
}
