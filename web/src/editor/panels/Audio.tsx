import { useMemo, useState } from 'react'
import { formatTime, programMap } from '../../render/timeline'
import { useEditor } from '../../state/editorStore'
import { Icon } from '../../ui/Icon'
import { Button, Slider, Switch, cx, toast } from '../../ui/kit'
import { clearCuts, restoreRange } from '../ops'
import { useRuntime } from '../runtime'

export function AudioPanel() {
  const rt = useRuntime()
  const tl = useEditor((s) => s.timeline)
  const edit = useEditor((s) => s.edit)
  const busy = useEditor((s) => s.busyAction)
  const runAction = useEditor((s) => s.runAction)
  const [silence, setSilence] = useState(0.6)
  const segments = tl?.segments

  const cuts = useMemo(() => {
    if (!segments) return []
    const map = programMap({ segments })
    return segments.flatMap((seg) =>
      seg.cuts.map((c) => {
        const before = map.find((r) => r.source_id === seg.source_id && Math.abs(r.src_end - c.start) < 0.01)
        return { ...c, source_id: seg.source_id, at: before ? before.at + (before.src_end - before.src_start) : 0, len: c.end - c.start }
      }),
    )
  }, [segments])
  const totalCut = cuts.reduce((a, c) => a + c.len, 0)

  if (!tl) return null

  const run = async () => {
    const before = cuts.length
    const ok = await runAction('remove-fillers', { silence_min: silence })
    if (ok) {
      const after = useEditor.getState().timeline!.segments.reduce((a, s) => a + s.cuts.length, 0)
      toast(after > before ? `Removed ${after - before} filler words and pauses` : 'No more fillers or long pauses found', 'ok')
    }
  }

  return (
    <div className="panel-body">
      <h3 className="sub tight">Clean up speech</h3>
      <p className="muted small">Finds filler words like um and uh, and long silences, and cuts them. Every cut can be restored below.</p>
      <Slider label="Shortest pause" value={silence} min={0.3} max={1.5} step={0.05} format={(v) => `${v.toFixed(2)} s`} onChange={setSilence} />
      <Button kind="primary" icon="wand" busy={busy === 'remove-fillers'} disabled={!!busy} onClick={() => void run()}>
        Remove fillers and pauses
      </Button>

      <h3 className="sub row-between">
        <span>Cuts</span>
        {cuts.length > 0 && (
          <button className="mini-link" onClick={() => edit((t) => clearCuts(t))}>
            Restore all
          </button>
        )}
      </h3>
      {cuts.length === 0 ? (
        <p className="muted small">Nothing has been cut. Select words in the transcript to cut them by hand.</p>
      ) : (
        <>
          <p className="muted small">
            {cuts.length} cut{cuts.length === 1 ? '' : 's'} removing {totalCut.toFixed(1)} s.
          </p>
          <ul className="cut-list">
            {cuts.map((c) => (
              <li key={`${c.source_id}-${c.start}`}>
                <button className="cut-main" onClick={() => rt?.engine.seek(Math.max(c.at - 0.6, 0))} title="Jump to this cut">
                  <span className={cx('reason', `r-${c.reason}`)}>{c.reason === 'filler' ? 'Filler' : c.reason === 'silence' ? 'Pause' : 'Manual'}</span>
                  <span className="cut-time">{formatTime(c.at, true)}</span>
                  <span className="muted small">{c.len.toFixed(2)} s</span>
                </button>
                <button className="icon-btn" aria-label="Restore this cut" title="Restore this cut" onClick={() => edit((t) => restoreRange(t, c.source_id, c.start, c.end))}>
                  <Icon name="restore" size={16} />
                </button>
              </li>
            ))}
          </ul>
        </>
      )}

      <h3 className="sub">Sound</h3>
      <Switch checked={tl.audio.normalize} onChange={(v) => edit((t) => ({ ...t, audio: { ...t.audio, normalize: v } }))} label="Even out loudness on export" />
      <p className="muted small hint">Brings the clip to the level social apps expect (about minus 14 LUFS).</p>
    </div>
  )
}
