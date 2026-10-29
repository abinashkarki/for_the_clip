import { useEffect, useMemo, useRef, useState } from 'react'
import { effectiveShots, formatTime, programMap } from '../render/timeline'
import type { HookOverlay } from '../render/types'
import { useEditor } from '../state/editorStore'
import { Button, cx } from '../ui/kit'
import { LayoutGlyph } from '../ui/Icon'
import { getHook, mergeShotWithPrevious, moveShotBoundary, restoreRange, setHook, splitShot } from './ops'
import { useRuntime } from './runtime'

const GUTTER = 76
const LAYOUT_NAMES: Record<string, string> = { fill: 'Fill', fit: 'Fit', stack: 'Stack', screen_speaker: 'Screen + speaker' }

export function TimelineStrip() {
  const rt = useRuntime()
  const timeline = useEditor((s) => s.timeline)
  const words = useEditor((s) => s.words)
  const edit = useEditor((s) => s.edit)
  const wrapRef = useRef<HTMLDivElement>(null)
  const headRef = useRef<HTMLDivElement>(null)
  const [width, setWidth] = useState(800)
  const [zoom, setZoom] = useState(1)
  const [now, setNow] = useState(0)

  useEffect(() => {
    const el = wrapRef.current
    if (!el) return
    const ro = new ResizeObserver(() => setWidth(el.clientWidth))
    ro.observe(el)
    setWidth(el.clientWidth)
    return () => ro.disconnect()
  }, [])

  const segments = timeline?.segments
  const map = useMemo(() => (segments ? programMap({ segments }) : []), [segments])
  const duration = map.length ? map[map.length - 1].at + (map[map.length - 1].src_end - map[map.length - 1].src_start) : 0
  const avail = Math.max(width - GUTTER - 16, 200)
  const pps = duration > 0 ? (avail / duration) * zoom : 1
  const total = duration * pps
  const shots = timeline ? effectiveShots(timeline) : []
  const hook = timeline ? getHook(timeline) : null

  // playhead: direct DOM updates, cheap per frame
  useEffect(() => {
    if (!rt) return
    const update = () => {
      const t = rt.engine.time
      if (headRef.current) headRef.current.style.transform = `translateX(${t * pps}px)`
      setNow((prev) => (Math.abs(prev - t) > 0.05 ? t : prev))
      const wrap = wrapRef.current
      if (wrap && zoom > 1 && rt.engine.playing) {
        const x = t * pps
        const view = wrap.scrollLeft
        if (x < view + 20 || x > view + wrap.clientWidth - GUTTER - 60) wrap.scrollLeft = Math.max(0, x - 80)
      }
    }
    update()
    return rt.engine.subscribe(update)
  }, [rt, pps, zoom])

  const xToTime = (clientX: number) => {
    const wrap = wrapRef.current!
    const rect = wrap.getBoundingClientRect()
    return Math.min(Math.max((clientX - rect.left - GUTTER + wrap.scrollLeft) / pps, 0), duration)
  }

  const scrubbing = useRef(false)
  const onScrubDown = (e: React.PointerEvent) => {
    if ((e.target as HTMLElement).closest('[data-nodrag]')) return
    scrubbing.current = true
    ;(e.currentTarget as HTMLElement).setPointerCapture(e.pointerId)
    rt?.engine.seek(xToTime(e.clientX))
  }
  const onScrubMove = (e: React.PointerEvent) => {
    if (scrubbing.current) rt?.engine.seek(xToTime(e.clientX))
  }
  const onScrubUp = () => (scrubbing.current = false)

  // ruler ticks
  const step = pps > 90 ? 1 : pps > 40 ? 2 : pps > 18 ? 5 : 10
  const ticks: number[] = []
  for (let t = 0; t <= duration + 1e-6; t += step) ticks.push(t)

  // cut and join markers between kept ranges
  const joins = map.slice(1).map((r, i) => {
    const prev = map[i]
    const same = prev.segment_id === r.segment_id
    return { at: r.at, same, sourceId: r.source_id, start: prev.src_end, end: r.src_start, removed: r.src_start - prev.src_end }
  })
  const cutReason = (sid: string, s: number) => {
    for (const seg of timeline?.segments ?? []) {
      const c = seg.cuts.find((x) => seg.source_id === sid && Math.abs(x.start - s) < 0.01)
      if (c) return c.reason
    }
    return 'manual'
  }

  const startDrag = (onMove: (t: number) => void) => (e: React.PointerEvent) => {
    e.stopPropagation()
    const target = e.currentTarget as HTMLElement
    target.setPointerCapture(e.pointerId)
    const move = (ev: PointerEvent) => onMove(xToTime(ev.clientX))
    const up = () => {
      target.removeEventListener('pointermove', move)
      target.removeEventListener('pointerup', up)
      target.removeEventListener('pointercancel', up)
    }
    target.addEventListener('pointermove', move)
    target.addEventListener('pointerup', up)
    target.addEventListener('pointercancel', up)
  }

  const splitHere = () => rt && edit((tl) => splitShot(tl, rt.engine.time))
  const spoken = useMemo(() => {
    if (!timeline) return []
    return map.flatMap((r) =>
      words
        .filter((w) => w.source_id === r.source_id && (w.start + w.end) / 2 >= r.src_start && (w.start + w.end) / 2 < r.src_end)
        .map((w) => ({
          key: w.key,
          left: r.at + Math.max(w.start, r.src_start) - r.src_start,
          w: Math.min(w.end, r.src_end) - Math.max(w.start, r.src_start),
          hidden: timeline.captions.hidden.includes(w.key),
          emph: timeline.captions.emphasis.includes(w.key),
        })),
    )
  }, [map, words, timeline])

  if (!timeline) return null
  const currentShot = shots.findIndex((s) => now < s.end - 1e-6)

  return (
    <section className="timeline" aria-label="Timeline">
      <div className="tl-toolbar">
        <div className="tl-title">Timeline <span className="muted">{formatTime(duration, true)}</span></div>
        <div className="tl-tools">
          <Button size="sm" icon="split" onClick={splitHere} title="Split the shot at the playhead (S)">
            Split shot
          </Button>
          {shots.length > 1 && currentShot > 0 && (
            <Button size="sm" kind="ghost" onClick={() => edit((tl) => mergeShotWithPrevious(tl, shots[currentShot].id))} title="Join this shot with the one before it">
              Join with previous
            </Button>
          )}
          <label className="tl-zoom">
            <span className="muted small">Zoom</span>
            <input type="range" min={1} max={6} step={0.1} value={zoom} onChange={(e) => setZoom(Number(e.target.value))} aria-label="Timeline zoom" />
          </label>
        </div>
      </div>

      <div className="tl-scroll" ref={wrapRef} onPointerDown={onScrubDown} onPointerMove={onScrubMove} onPointerUp={onScrubUp} onPointerCancel={onScrubUp}>
        <div className="tl-inner" style={{ width: GUTTER + total + 16 }}>
          <div className="tl-labels">
            <div className="tl-lab ruler-lab" />
            <div className="tl-lab">Shots</div>
            <div className="tl-lab">Speech</div>
            <div className="tl-lab">Hook</div>
          </div>
          <div className="tl-tracks" style={{ width: total }}>
            <div className="tl-ruler" role="slider" aria-label="Playhead" aria-valuemin={0} aria-valuemax={Math.round(duration * 100) / 100} aria-valuenow={Math.round(now * 100) / 100} aria-valuetext={formatTime(now, true)}>
              {ticks.map((t) => (
                <span key={t} className="tick" style={{ left: t * pps }}>
                  <i />
                  <b>{formatTime(t)}</b>
                </span>
              ))}
            </div>

            <div className="tl-track tl-shots">
              {shots.map((s, i) => (
                <div
                  key={s.id}
                  className={cx('shot', `shot-${s.layout}`, i === currentShot && 'is-current')}
                  style={{ left: s.start * pps, width: Math.max((s.end - s.start) * pps - 2, 2) }}
                  title={`${LAYOUT_NAMES[s.layout]} · ${formatTime(s.start, true)} to ${formatTime(s.end, true)}`}
                >
                  <LayoutGlyph kind={s.layout} size={20} />
                  <span>{LAYOUT_NAMES[s.layout]}</span>
                </div>
              ))}
              {shots.slice(0, -1).map((s, i) => (
                <div
                  key={`b-${s.id}`}
                  data-nodrag
                  className="shot-handle"
                  style={{ left: s.end * pps - 5 }}
                  onPointerDown={startDrag((t) => edit((tl) => moveShotBoundary(tl, i, t), { coalesce: 'boundary' }))}
                  title="Drag to move the shot boundary"
                />
              ))}
            </div>

            <div className="tl-track tl-speech">
              {spoken.map((w) => (
                <i key={w.key} className={cx('wbar', w.hidden && 'is-hidden', w.emph && 'is-emph')} style={{ left: w.left * pps, width: Math.max(w.w * pps - 1, 1) }} />
              ))}
              {joins.map((j) => (
                <button
                  key={`${j.at}`}
                  data-nodrag
                  className={cx('join', j.same ? 'is-cut' : 'is-seg')}
                  style={{ left: j.at * pps }}
                  title={j.same ? `Cut: ${j.removed.toFixed(2)} s removed (${cutReason(j.sourceId, j.start)}). Click to restore.` : `Jump to a later part of the video (${j.removed.toFixed(1)} s skipped)`}
                  aria-label={j.same ? `Restore ${j.removed.toFixed(2)} second cut` : 'Part boundary'}
                  onClick={() => j.same && edit((tl) => restoreRange(tl, j.sourceId, j.start, j.end))}
                />
              ))}
            </div>

            <div className="tl-track tl-hook">
              {hook ? (
                <HookBar hook={hook} pps={pps} duration={duration} startDrag={startDrag} />
              ) : (
                <span className="tl-ghost">No hook. Add one in the Hook tab.</span>
              )}
            </div>

            <div className="playhead" ref={headRef} aria-hidden="true">
              <span className="ph-flag" />
            </div>
          </div>
        </div>
      </div>
    </section>
  )
}

function HookBar({ hook, pps, duration, startDrag }: { hook: HookOverlay; pps: number; duration: number; startDrag: (f: (t: number) => void) => (e: React.PointerEvent) => void }) {
  const edit = useEditor((s) => s.edit)
  const origin = useRef({ start: hook.start, end: hook.end, grab: 0 })
  return (
    <div className="hook-bar" style={{ left: hook.start * pps, width: Math.max((hook.end - hook.start) * pps, 8) }} data-nodrag>
      <div
        className="hook-body"
        onPointerDown={(e) => {
          origin.current = { start: hook.start, end: hook.end, grab: 0 }
          let first = true
          startDrag((t) => {
            if (first) {
              origin.current.grab = t - hook.start
              first = false
            }
            const len = origin.current.end - origin.current.start
            const s = Math.min(Math.max(t - origin.current.grab, 0), Math.max(duration - len, 0))
            edit((tl) => setHook(tl, { start: s, end: s + len }), { coalesce: 'hook-move' })
          })(e)
        }}
        title="Drag to move the hook"
      >
        <span>{hook.text}</span>
      </div>
      <div className="hook-edge is-l" onPointerDown={startDrag((t) => edit((tl) => setHook(tl, { start: Math.min(t, hook.end - 0.3) }), { coalesce: 'hook-l' }))} title="Drag to change when the hook appears" />
      <div className="hook-edge is-r" onPointerDown={startDrag((t) => edit((tl) => setHook(tl, { end: Math.max(t, hook.start + 0.3) }), { coalesce: 'hook-r' }))} title="Drag to change when the hook disappears" />
    </div>
  )
}
