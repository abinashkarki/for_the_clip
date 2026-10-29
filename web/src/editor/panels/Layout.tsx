import { useEffect, useRef, useState } from 'react'
import { placeRegions, framingAt } from '../../render/layout'
import { effectiveShots, formatTime } from '../../render/timeline'
import type { LayoutKind } from '../../render/types'
import { useEditor } from '../../state/editorStore'
import { LayoutGlyph } from '../../ui/Icon'
import { Button, ColorField, Segmented, Slider, cx } from '../../ui/kit'
import { addKeyframe, clearKeyframes, setBackground, setRegionFraming, setShotLayout, shotIndexAt } from '../ops'
import { useEngineValue, useRuntime } from '../runtime'

const LAYOUTS: { id: LayoutKind; name: string; hint: string }[] = [
  { id: 'fill', name: 'Fill', hint: 'Crop to fill the frame' },
  { id: 'fit', name: 'Fit', hint: 'Whole picture, blurred sides' },
  { id: 'stack', name: 'Stack', hint: 'Two people, top and bottom' },
  { id: 'screen_speaker', name: 'Screen + speaker', hint: 'Slides above, speaker below' },
]
const REGION_NAMES: Record<LayoutKind, string[]> = {
  fill: ['Picture'],
  fit: ['Picture'],
  stack: ['Top', 'Bottom'],
  screen_speaker: ['Screen', 'Speaker'],
}

export function LayoutPanel() {
  const rt = useRuntime()
  const tl = useEditor((s) => s.timeline)
  const edit = useEditor((s) => s.edit)
  const busy = useEditor((s) => s.busyAction)
  const runAction = useEditor((s) => s.runAction)
  const tick = useEngineValue(rt, (r) => Math.floor(r.engine.time * 10))
  if (!tl || !rt) return null
  const t = (tick ?? 0) / 10
  const shots = effectiveShots(tl)
  const idx = shotIndexAt(shots, t)
  const shot = shots[idx]
  const names = REGION_NAMES[shot.layout]

  return (
    <div className="panel-body">
      <div className="row-between">
        <h3 className="sub tight">
          Shot {idx + 1} of {shots.length}
        </h3>
        <span className="muted small">{formatTime(shot.start, true)} to {formatTime(shot.end, true)}</span>
      </div>
      <div className="layout-grid" role="radiogroup" aria-label="Layout for this shot">
        {LAYOUTS.map((l) => (
          <button key={l.id} role="radio" aria-checked={shot.layout === l.id} className={cx('layout-opt', shot.layout === l.id && 'is-on')} onClick={() => edit((x) => setShotLayout(x, shot.id, l.id))} title={l.hint}>
            <LayoutGlyph kind={l.id} size={46} />
            <span>{l.name}</span>
          </button>
        ))}
      </div>
      <p className="muted small hint">{LAYOUTS.find((l) => l.id === shot.layout)!.hint}. Drag the preview to reframe, or use the map below.</p>

      <SourceMap />

      {shot.regions.map((r, i) => {
        const f = framingAt(shot, i, t)
        const posless = shot.layout === 'fit' || (shot.layout === 'screen_speaker' && i === 0)
        return (
          <fieldset key={i} className="region">
            <legend>{names[i] ?? `Region ${i + 1}`}</legend>
            {!posless && (
              <>
                <Slider label="Across" value={Math.round(f.x * 100)} min={0} max={100} suffix="%" onChange={(v) => edit((x) => setRegionFraming(x, shot.id, i, t, { x: v / 100 }), { coalesce: `rx${i}` })} />
                <Slider label="Down" value={Math.round(f.y * 100)} min={0} max={100} suffix="%" onChange={(v) => edit((x) => setRegionFraming(x, shot.id, i, t, { y: v / 100 }), { coalesce: `ry${i}` })} />
              </>
            )}
            <Slider label="Zoom" value={Number(f.zoom.toFixed(2))} min={1} max={3} step={0.01} format={(v) => `${v.toFixed(2)}x`} onChange={(v) => edit((x) => setRegionFraming(x, shot.id, i, t, { zoom: v }), { coalesce: `rz${i}` })} />
            <div className="row-between small muted">
              <span>{r.keyframes.length > 1 ? `${r.keyframes.length} keyframes` : 'Fixed framing'}</span>
              <span className="btn-row">
                {r.keyframes.length <= 1 ? (
                  <button className="mini-link" onClick={() => edit((x) => addKeyframe(x, shot.id, i, shot.end - 0.05))}>
                    Animate across shot
                  </button>
                ) : (
                  <button className="mini-link" onClick={() => edit((x) => clearKeyframes(x, shot.id, t))}>
                    Clear keyframes
                  </button>
                )}
              </span>
            </div>
          </fieldset>
        )
      })}

      <h3 className="sub">Background</h3>
      <Segmented
        label="Background"
        value={tl.canvas.background.kind}
        onChange={(k) => edit((x) => setBackground(x, k))}
        options={[{ value: 'blur', label: 'Blurred picture' }, { value: 'color', label: 'Solid colour' }]}
      />
      {tl.canvas.background.kind === 'color' && (
        <div className="stack">
          <ColorField label="Colour" value={tl.canvas.background.color} onChange={(c) => edit((x) => setBackground(x, 'color', c), { coalesce: 'bg' })} />
        </div>
      )}

      <h3 className="sub">Auto-reframe</h3>
      <p className="muted small">Follows the speaker through the whole clip and picks a layout for each part. This replaces the current shots; you can undo it.</p>
      <Button icon="wand" busy={busy === 'auto-reframe'} disabled={!!busy} onClick={() => void runAction('auto-reframe')}>
        {busy === 'auto-reframe' ? 'Analysing…' : 'Auto-reframe clip'}
      </Button>
    </div>
  )
}

/** The whole source frame with each region's crop window; drag a window to reframe. */
function SourceMap() {
  const rt = useRuntime()
  const edit = useEditor((s) => s.edit)
  const ref = useRef<HTMLCanvasElement>(null)
  const [aspect, setAspect] = useState(16 / 9)
  const drag = useRef<{ region: number; shotId: string; sx: number; sy: number; cx: number; cy: number } | null>(null)

  useEffect(() => {
    if (!rt) return
    const paint = () => {
      const canvas = ref.current
      const s = useEditor.getState()
      const media = rt.engine.frameSource as unknown as HTMLVideoElement | null
      if (!canvas || !s.timeline || !media) return
      const vw = media.videoWidth || 16
      const vh = media.videoHeight || 9
      setAspect((a) => (Math.abs(a - vw / vh) > 0.001 ? vw / vh : a))
      const ctx = canvas.getContext('2d')!
      const W = canvas.width
      const H = canvas.height
      ctx.fillStyle = '#0b0d11'
      ctx.fillRect(0, 0, W, H)
      if (media.readyState >= 2 && media.videoWidth) ctx.drawImage(media, 0, 0, W, H)
      const t = rt.engine.time
      const shot = effectiveShots(s.timeline).find((x) => t < x.end) ?? effectiveShots(s.timeline).at(-1)!
      const placed = placeRegions(shot, { w: vw, h: vh }, 540, 960, t)
      ctx.save()
      ctx.fillStyle = 'rgba(8,10,14,0.55)'
      ctx.beginPath()
      ctx.rect(0, 0, W, H)
      const k = W / vw
      for (const p of placed) ctx.rect(p.source.x * k, p.source.y * k, p.source.w * k, p.source.h * k)
      ctx.fill('evenodd')
      ctx.restore()
      placed.forEach((p, i) => {
        ctx.lineWidth = 2
        ctx.strokeStyle = i === 0 ? '#8ea2ff' : '#ffc247'
        ctx.strokeRect(p.source.x * k + 1, p.source.y * k + 1, p.source.w * k - 2, p.source.h * k - 2)
      })
    }
    paint()
    return rt.bus.subscribe(paint)
  }, [rt])

  const hit = (e: React.PointerEvent) => {
    const canvas = ref.current!
    const media = rt?.engine.frameSource as unknown as HTMLVideoElement | null
    const s = useEditor.getState()
    if (!media || !s.timeline) return null
    const vw = media.videoWidth || 16
    const vh = media.videoHeight || 9
    const rect = canvas.getBoundingClientRect()
    const k = vw / rect.width
    const px = (e.clientX - rect.left) * k
    const py = (e.clientY - rect.top) * k
    const t = rt!.engine.time
    const shot = effectiveShots(s.timeline).find((x) => t < x.end) ?? effectiveShots(s.timeline).at(-1)!
    const placed = placeRegions(shot, { w: vw, h: vh }, 540, 960, t)
    return { px, py, k, vw, vh, shot, placed, t }
  }

  const onDown = (e: React.PointerEvent) => {
    const h = hit(e)
    if (!h) return
    let region = h.placed.findIndex((p) => h.px >= p.source.x && h.px <= p.source.x + p.source.w && h.py >= p.source.y && h.py <= p.source.y + p.source.h)
    if (region < 0) region = 0
    const p = h.placed[region]
    if (p.mode === 'fit') return
    e.currentTarget.setPointerCapture(e.pointerId)
    drag.current = { region, shotId: h.shot.id, sx: h.px, sy: h.py, cx: p.source.x + p.source.w / 2, cy: p.source.y + p.source.h / 2 }
    // clicking outside any window recentres it there
    if (!h.placed.some((q) => h.px >= q.source.x && h.px <= q.source.x + q.source.w && h.py >= q.source.y && h.py <= q.source.y + q.source.h)) {
      drag.current.cx = h.px
      drag.current.cy = h.py
      edit((tl) => setRegionFraming(tl, h.shot.id, region, h.t, { x: h.px / h.vw, y: h.py / h.vh }), { coalesce: 'map' })
    }
  }
  const onMove = (e: React.PointerEvent) => {
    const d = drag.current
    if (!d) return
    const h = hit(e)
    if (!h) return
    const nx = (d.cx + (h.px - d.sx)) / h.vw
    const ny = (d.cy + (h.py - d.sy)) / h.vh
    edit((tl) => setRegionFraming(tl, d.shotId, d.region, h.t, { x: nx, y: ny }), { coalesce: 'map' })
  }

  return (
    <div className="source-map" style={{ aspectRatio: String(aspect) }}>
      <canvas ref={ref} width={320} height={Math.round(320 / aspect)} onPointerDown={onDown} onPointerMove={onMove} onPointerUp={() => (drag.current = null)} onPointerCancel={() => (drag.current = null)} aria-label="Source frame with crop windows. Drag a window to reframe." />
      <span className="map-label">Source frame</span>
    </div>
  )
}
