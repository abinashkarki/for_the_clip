import { useEffect, useRef, useState } from 'react'
import { Compositor } from '../render/compose'
import { placeRegions, shotAt } from '../render/layout'
import { effectiveShots, formatTime } from '../render/timeline'
import type { MediaLike } from '../render/playback'
import { useEditor } from '../state/editorStore'
import { useRegistry } from '../state/registry'
import { Icon } from '../ui/Icon'
import { IconButton, cx } from '../ui/kit'
import { setRegionFraming } from './ops'
import { useEngineValue, useRuntime } from './runtime'

const MAX_DPR = 2

export function PreviewStage() {
  const rt = useRuntime()
  const registry = useRegistry()
  const timeline = useEditor((s) => s.timeline)
  const detail = useEditor((s) => s.detail)
  const edit = useEditor((s) => s.edit)
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const frameRef = useRef<HTMLDivElement>(null)
  const compositor = useRef(new Compositor())
  const lastDraw = useRef(0)
  const regRef = useRef(registry)
  regRef.current = registry
  const [hint, setHint] = useState(false)
  const canvasSpec = timeline?.canvas

  // size backing store to CSS size x DPR
  useEffect(() => {
    const frame = frameRef.current
    const canvas = canvasRef.current
    if (!frame || !canvas || !canvasSpec) return
    const apply = () => {
      const dpr = Math.min(window.devicePixelRatio || 1, MAX_DPR)
      const w = Math.max(2, Math.round(frame.clientWidth * dpr))
      const h = Math.round((w * canvasSpec.height) / canvasSpec.width)
      if (canvas.width !== w || canvas.height !== h) {
        canvas.width = w
        canvas.height = h
        rt?.bus.request()
      }
    }
    apply()
    const ro = new ResizeObserver(apply)
    ro.observe(frame)
    return () => ro.disconnect()
  }, [canvasSpec?.width, canvasSpec?.height, rt])

  // paint
  useEffect(() => {
    if (!rt) return
    const paint = () => {
      const canvas = canvasRef.current
      const s = useEditor.getState()
      if (!canvas || !s.timeline || !s.detail) return
      const now = performance.now()
      if (rt.engine.playing && now - lastDraw.current < 28) {
        rt.bus.request()
        return
      }
      lastDraw.current = now
      const ctx = canvas.getContext('2d')
      if (!ctx) return
      const media = rt.engine.frameSource as (MediaLike & HTMLVideoElement) | null
      const t = rt.engine.time
      const sourceId = Object.keys(s.detail.sources)[0]
      const info = s.detail.sources[sourceId]
      const ready = media && media.readyState >= 2 && media.videoWidth > 0
      const { words, boundaries, version } = rt.getWords()
      compositor.current.draw(ctx, canvas.width, canvas.height, {
        timeline: s.timeline,
        t,
        duration: rt.engine.duration,
        picture: ready ? { img: media!, src: { w: media!.videoWidth, h: media!.videoHeight } } : null,
        registry: regRef.current,
        words,
        boundaries,
        version,
        logos: rt.logos,
      })
      void info
    }
    const off = rt.bus.subscribe(paint)
    paint()
    return off
  }, [rt, registry])

  // pointer drag = reframe, wheel = zoom
  const drag = useRef<{ shotId: string; region: number; sx: number; sy: number; cx: number; cy: number; kx: number; ky: number } | null>(null)

  const locate = (_clientX: number, clientY: number) => {
    const canvas = canvasRef.current
    const s = useEditor.getState()
    const media = rt?.engine.frameSource as unknown as HTMLVideoElement | null
    if (!canvas || !rt || !s.timeline || !media || !media.videoWidth) return null
    const rect = canvas.getBoundingClientRect()
    const t = rt.engine.time
    const shot = shotAt(effectiveShots(s.timeline), t)
    const placed = placeRegions(shot, { w: media.videoWidth, h: media.videoHeight }, canvas.width, canvas.height, t)
    const py = ((clientY - rect.top) / rect.height) * canvas.height
    const region = placed.find((p) => py >= p.slot.y && py < p.slot.y + p.slot.h) ?? placed[0]
    return { shot, region, rect, canvasW: canvas.width, t, src: { w: media.videoWidth, h: media.videoHeight } }
  }

  const onDown = (e: React.PointerEvent) => {
    if (e.button !== 0) return
    const loc = locate(e.clientX, e.clientY)
    if (!loc || loc.region.mode !== 'crop') return
    e.currentTarget.setPointerCapture(e.pointerId)
    const r = loc.region
    drag.current = {
      shotId: loc.shot.id,
      region: r.index,
      sx: e.clientX,
      sy: e.clientY,
      cx: r.source.x + r.source.w / 2,
      cy: r.source.y + r.source.h / 2,
      kx: r.source.w / r.dest.w,
      ky: r.source.h / r.dest.h,
    }
  }
  const onMove = (e: React.PointerEvent) => {
    const d = drag.current
    if (!d) return
    const loc = locate(e.clientX, e.clientY)
    if (!loc) return
    const k = loc.canvasW / loc.rect.width
    const nx = (d.cx - (e.clientX - d.sx) * k * d.kx) / loc.src.w
    const ny = (d.cy - (e.clientY - d.sy) * k * d.ky) / loc.src.h
    edit((tl) => setRegionFraming(tl, d.shotId, d.region, loc.t, { x: nx, y: ny }), { coalesce: 'drag' })
  }
  const onUp = () => {
    drag.current = null
  }

  useEffect(() => {
    const el = frameRef.current
    if (!el || !rt) return
    const onWheel = (e: WheelEvent) => {
      const loc = locate(e.clientX, e.clientY)
      if (!loc || loc.shot.layout === 'fit') return
      e.preventDefault()
      const z = loc.region.framing.zoom * Math.exp(-e.deltaY * 0.0015)
      useEditor.getState().edit((tl) => setRegionFraming(tl, loc.shot.id, loc.region.index, loc.t, { zoom: z }), { coalesce: 'wheel' })
    }
    el.addEventListener('wheel', onWheel, { passive: false })
    return () => el.removeEventListener('wheel', onWheel)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rt])

  const layout = useEngineValue(rt, (r) => shotAt(effectiveShots(useEditor.getState().timeline!), r.engine.time).layout)
  const canReframe = layout && layout !== 'fit'
  const aspect = canvasSpec ? `${canvasSpec.width} / ${canvasSpec.height}` : '9 / 16'
  void detail

  return (
    <div className="stage">
      <div
        ref={frameRef}
        className={cx('stage-frame', canReframe && 'can-reframe')}
        style={{ aspectRatio: aspect, ['--inv' as string]: canvasSpec ? canvasSpec.height / canvasSpec.width : 1.7778 }}
        onPointerEnter={() => setHint(true)}
        onPointerLeave={() => setHint(false)}
      >
        <canvas ref={canvasRef} className="stage-canvas" aria-label="Clip preview" />
        <div className="stage-hit" onPointerDown={onDown} onPointerMove={onMove} onPointerUp={onUp} onPointerCancel={onUp} onDoubleClick={() => rt?.engine.toggle()} />
        {canReframe && hint && !drag.current && <div className="stage-hint">Drag to reframe, scroll to zoom</div>}
      </div>
    </div>
  )
}

export function Transport() {
  const rt = useRuntime()
  const playing = useEngineValue(rt, (r) => r.engine.playing)
  const time = useEngineValue(rt, (r) => Math.floor(r.engine.time * 20))
  const rate = useEngineValue(rt, (r) => r.engine.playbackRate) ?? 1
  const [muted, setMuted] = useState(false)
  const duration = rt?.engine.duration ?? 0
  const t = (time ?? 0) / 20
  useEffect(() => {
    rt?.engine.setMuted(muted)
  }, [rt, muted])
  if (!rt) return <div className="transport" />
  const step = (d: number) => rt.engine.seek(rt.engine.time + d)
  return (
    <div className="transport" role="group" aria-label="Playback">
      <IconButton icon="back" label="Back one second (Left arrow, shift)" onClick={() => step(-1)} />
      <button className="play-btn" onClick={() => rt.engine.toggle()} aria-label={playing ? 'Pause (Space)' : 'Play (Space)'} title={playing ? 'Pause (Space)' : 'Play (Space)'}>
        <Icon name={playing ? 'pause' : 'play'} size={20} />
      </button>
      <IconButton icon="chevron" label="Forward one second" onClick={() => step(1)} />
      <div className="timecode" aria-live="off">
        <span>{formatTime(t, true)}</span>
        <span className="muted"> / {formatTime(duration, true)}</span>
      </div>
      <button className="rate-btn" onClick={() => rt.engine.setRate(rate === 1 ? 1.5 : rate === 1.5 ? 2 : rate === 2 ? 0.5 : 1)} title="Playback speed (L increases)">
        {rate}x
      </button>
      <IconButton icon={muted ? 'mute' : 'volume'} label={muted ? 'Unmute' : 'Mute'} onClick={() => setMuted((m) => !m)} />
    </div>
  )
}
