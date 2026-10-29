// Drives hidden <video> elements of the source(s) along the programme map.
// Every source gets two elements: the active one plays the current kept range while the standby one
// is pre-seeked to the start of the next range, so crossing a cut or segment boundary is a swap, not a seek.
import { EPS, programToSource } from './timeline'
import type { ProgramRange } from './types'

export interface MediaLike {
  currentTime: number
  readonly paused: boolean
  readonly seeking: boolean
  readonly readyState: number
  readonly videoWidth: number
  readonly videoHeight: number
  muted: boolean
  playbackRate?: number
  play(): Promise<void> | void
  pause(): void
}

export interface PlaybackOptions {
  createMedia: (sourceId: string) => MediaLike
  /** swap this long (s) before the end of a range to absorb frame-timer jitter */
  lead?: number
}

interface Slot {
  media: MediaLike
  index: number
}

const SEEK_SLOP = 0.02

export class PlaybackEngine {
  private ranges: ProgramRange[] = []
  private total = 0
  private pools = new Map<string, MediaLike[]>()
  private active: Slot | null = null
  private standby: Slot | null = null
  private t = 0
  private _playing = false
  private raf = 0
  private listeners = new Set<() => void>()
  private readonly lead: number
  private muted = false
  private rate = 1

  constructor(private opts: PlaybackOptions) {
    this.lead = opts.lead ?? 0.03
  }

  // ---- state
  get playing() {
    return this._playing
  }
  get duration() {
    return this.total
  }
  get ended() {
    return this.total > 0 && this.t >= this.total - 0.001 && !this._playing
  }

  /** Current programme time. */
  get time(): number {
    if (this._playing && this.active) {
      const r = this.ranges[this.active.index]
      if (r) {
        const local = this.active.media.currentTime - r.src_start
        return Math.min(Math.max(r.at + local, r.at), this.total)
      }
    }
    return this.t
  }

  /** The element whose frame should be drawn for the current time. */
  get frameSource(): MediaLike | null {
    return this.active?.media ?? null
  }

  subscribe(cb: () => void): () => void {
    this.listeners.add(cb)
    return () => this.listeners.delete(cb)
  }
  private emit() {
    this.listeners.forEach((l) => l())
  }

  setMuted(m: boolean) {
    this.muted = m
    this.pools.forEach((p) => p.forEach((media) => (media.muted = m)))
  }

  setRate(r: number) {
    this.rate = r
    this.pools.forEach((p) => p.forEach((media) => (media.playbackRate = r)))
    this.emit()
  }
  get playbackRate() {
    return this.rate
  }

  // ---- media pool
  private pool(sourceId: string): MediaLike[] {
    let p = this.pools.get(sourceId)
    if (!p) {
      p = [this.opts.createMedia(sourceId), this.opts.createMedia(sourceId)]
      p.forEach((m) => {
        m.muted = this.muted
        m.playbackRate = this.rate
      })
      this.pools.set(sourceId, p)
    }
    return p
  }

  // ---- programme
  /** Replace the programme map (after an edit). Keeps the programme time, resumes if it was playing. */
  setRanges(ranges: ProgramRange[]) {
    const wasPlaying = this._playing
    const keep = this.time
    this.ranges = ranges
    this.total = ranges.reduce((a, r) => a + (r.src_end - r.src_start), 0)
    this.standby = null
    this.t = Math.min(keep, this.total)
    if (!ranges.length) {
      this.pauseAll()
      this.active = null
      this._playing = false
      this.emit()
      return
    }
    this.place(this.t, wasPlaying)
    this.emit()
  }

  private pauseAll() {
    this.pools.forEach((p) => p.forEach((m) => !m.paused && m.pause()))
  }

  /** Put the active element on programme time t. */
  private place(t: number, play: boolean) {
    const pt = programToSource(this.ranges, t)
    if (!pt) return
    const pool = this.pool(pt.source_id)
    let media = this.active && pool.includes(this.active.media) ? this.active.media : pool[0]
    if (this.standby && pool.includes(this.standby.media) && this.standby.index === pt.index) {
      media = this.standby.media // already parked at the right place
    }
    if (this.active && this.active.media !== media) this.active.media.pause()
    if (Math.abs(media.currentTime - pt.time) > SEEK_SLOP || media.paused === false) {
      media.currentTime = pt.time
    }
    this.active = { media, index: pt.index }
    this.t = t
    if (this.standby?.media === media) this.standby = null
    if (play) this.startMedia(media)
    else if (!media.paused) media.pause()
    this.prepareStandby()
  }

  private startMedia(media: MediaLike) {
    try {
      const p = media.play()
      if (p && typeof (p as Promise<void>).catch === 'function') {
        ;(p as Promise<void>).catch(() => {
          this._playing = false
          this.emit()
        })
      }
    } catch {
      this._playing = false
    }
  }

  private prepareStandby() {
    if (!this.active) return
    const nextIndex = this.active.index + 1
    const next = this.ranges[nextIndex]
    if (!next) {
      this.standby = null
      return
    }
    const cur = this.ranges[this.active.index]
    const contiguous = next.source_id === cur.source_id && Math.abs(next.src_start - cur.src_end) < EPS
    if (contiguous) {
      this.standby = null
      return
    }
    const pool = this.pool(next.source_id)
    const media = pool.find((m) => m !== this.active!.media) ?? pool[0]
    if (this.standby && this.standby.media === media && this.standby.index === nextIndex) return
    if (media === this.active.media) return
    if (!media.paused) media.pause()
    if (Math.abs(media.currentTime - next.src_start) > SEEK_SLOP) media.currentTime = next.src_start
    this.standby = { media, index: nextIndex }
  }

  // ---- transport
  play() {
    if (!this.ranges.length) return
    const start = this.t >= this.total - 0.001 ? 0 : this.time
    this._playing = true
    this.place(start, true)
    this.startLoop()
    this.emit()
  }

  pause() {
    if (!this._playing) return
    this.t = this.time
    this._playing = false
    this.pauseAll()
    this.stopLoop()
    this.emit()
  }

  toggle() {
    if (this._playing) this.pause()
    else this.play()
  }

  seek(t: number) {
    if (!this.ranges.length) {
      this.t = 0
      return
    }
    const clamped = Math.min(Math.max(t, 0), this.total)
    this.standby = null
    this.place(clamped, this._playing)
    this.emit()
  }

  /** One scheduler step. Called per animation frame; public so tests can drive it. */
  tick() {
    const a = this.active
    if (!this._playing || !a) return
    const r = this.ranges[a.index]
    if (!r) return
    const ct = a.media.currentTime
    if (ct >= r.src_end - this.lead) {
      this.advance()
    } else if (a.media.paused && !a.media.seeking && a.media.readyState >= 2) {
      this.startMedia(a.media)
    }
    this.emit()
  }

  private advance() {
    const a = this.active!
    const nextIndex = a.index + 1
    const next = this.ranges[nextIndex]
    if (!next) {
      this.t = this.total
      this._playing = false
      this.pauseAll()
      this.stopLoop()
      return
    }
    const cur = this.ranges[a.index]
    const contiguous = next.source_id === cur.source_id && Math.abs(next.src_start - cur.src_end) < EPS
    if (contiguous) {
      this.active = { media: a.media, index: nextIndex }
      this.prepareStandby()
      return
    }
    const s = this.standby
    if (s && s.index === nextIndex) {
      if (Math.abs(s.media.currentTime - next.src_start) > SEEK_SLOP) s.media.currentTime = next.src_start
      this.startMedia(s.media)
      a.media.pause()
      this.active = { media: s.media, index: nextIndex }
      this.standby = null
      this.t = next.at
      this.prepareStandby()
    } else {
      this.place(next.at, true)
    }
  }

  // ---- frame loop
  private startLoop() {
    if (this.raf || typeof requestAnimationFrame === 'undefined') return
    const step = () => {
      this.tick()
      this.raf = this._playing ? requestAnimationFrame(step) : 0
    }
    this.raf = requestAnimationFrame(step)
  }
  private stopLoop() {
    if (this.raf && typeof cancelAnimationFrame !== 'undefined') cancelAnimationFrame(this.raf)
    this.raf = 0
  }

  dispose() {
    this.stopLoop()
    this.pauseAll()
    this.pools.forEach((p) => p.forEach((m) => ((m as unknown as HTMLVideoElement).removeAttribute?.('src'))))
    this.pools.clear()
    this.listeners.clear()
  }
}

/** Create a hidden, preloaded <video> for a source URL. */
export function createVideoElement(url: string): HTMLVideoElement {
  const v = document.createElement('video')
  v.preload = 'auto'
  v.playsInline = true
  v.crossOrigin = 'anonymous'
  v.src = url
  v.style.cssText = 'position:fixed;left:-9999px;top:0;width:2px;height:2px;opacity:0;pointer-events:none'
  document.body.appendChild(v)
  return v
}
