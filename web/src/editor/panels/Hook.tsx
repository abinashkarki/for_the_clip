import { useEditor } from '../../state/editorStore'
import { useRegistry } from '../../state/registry'
import { formatTime } from '../../render/timeline'
import { Button, Switch, cx } from '../../ui/kit'
import { getHook, setHook } from '../ops'
import { HookThumb } from '../Thumbs'
import { useRuntime } from '../runtime'

export function HookPanel() {
  const rt = useRuntime()
  const reg = useRegistry()
  const tl = useEditor((s) => s.timeline)
  const detail = useEditor((s) => s.detail)
  const edit = useEditor((s) => s.edit)
  if (!tl || !reg || !rt) return null
  const hook = getHook(tl)
  const dur = rt.engine.duration

  return (
    <div className="panel-body">
      <Switch
        checked={!!hook}
        onChange={(on) => edit((t) => setHook(t, on ? { text: detail?.clip.title.slice(0, 80) ?? 'Your hook here' } : null))}
        label="Show a hook at the start"
      />
      <p className="muted small hint">A hook is a line of text over the opening seconds that gives viewers a reason to keep watching.</p>

      {hook && (
        <>
          <div className="field">
            <label htmlFor="hook-text">Hook text</label>
            <textarea id="hook-text" className="input" rows={3} maxLength={120} value={hook.text} onChange={(e) => edit((t) => setHook(t, { text: e.target.value || ' ' }), { coalesce: 'hook-text' })} />
            <div className="row-between small muted">
              <button className="mini-link" onClick={() => edit((t) => setHook(t, { text: detail?.clip.title ?? hook.text }))}>
                Use the clip title
              </button>
              <span>{hook.text.length} / 120</span>
            </div>
          </div>

          <h3 className="sub">Style</h3>
          <div className="gallery hooks" role="listbox" aria-label="Hook styles">
            {reg.hooks.map((h) => (
              <button key={h.id} role="option" aria-selected={h.id === hook.style} className={cx('gal-item', h.id === hook.style && 'is-on')} onClick={() => edit((t) => setHook(t, { style: h.id }))}>
                <HookThumb style={h} text={hook.text} />
                <span className="gal-name">{h.name ?? h.id.replace(/^hook_/, '')}</span>
              </button>
            ))}
          </div>

          <h3 className="sub">Timing</h3>
          <div className="two">
            <label className="field">
              <span className="field-label">Appears at</span>
              <input className="input num" type="number" min={0} max={Math.max(dur - 0.2, 0)} step={0.1} value={Number(hook.start.toFixed(2))} onChange={(e) => edit((t) => setHook(t, { start: Number(e.target.value) }), { coalesce: 'hook-start' })} />
            </label>
            <label className="field">
              <span className="field-label">Disappears at</span>
              <input className="input num" type="number" min={0.2} max={dur} step={0.1} value={Number(hook.end.toFixed(2))} onChange={(e) => edit((t) => setHook(t, { end: Number(e.target.value) }), { coalesce: 'hook-end' })} />
            </label>
          </div>
          <div className="btn-row">
            <Button size="sm" onClick={() => edit((t) => setHook(t, { start: Math.min(rt.engine.time, hook.end - 0.3) }))}>
              Start at playhead
            </Button>
            <Button size="sm" onClick={() => edit((t) => setHook(t, { end: Math.max(rt.engine.time, hook.start + 0.3) }))}>
              End at playhead
            </Button>
          </div>
          <p className="muted small hint">Shows for {formatTime(hook.end - hook.start, true)}. Drag the hook bar in the timeline to move it.</p>
        </>
      )}
    </div>
  )
}
