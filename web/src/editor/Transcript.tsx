import { useEffect, useMemo, useRef, useState } from 'react'
import { formatTime, isWordRemoved, programMap, sourceToProgram } from '../render/timeline'
import type { SourceWord } from '../render/types'
import { useEditor } from '../state/editorStore'
import { Icon } from '../ui/Icon'
import { Button, cx } from '../ui/kit'
import { addCut, restoreRange, toggleEmphasis, toggleHiddenWord } from './ops'
import { useEngineValue, useRuntime } from './runtime'

const FILLERS = /^(um+|uh+|er+|erm|hmm+)$/i

interface Row {
  word: SourceWord
  index: number
  removed: boolean
}
interface Para {
  rows: Row[]
  /** label shown before the paragraph when a new segment begins */
  segment?: { index: number; at: number }
}

export function TranscriptPanel() {
  const rt = useRuntime()
  const timeline = useEditor((s) => s.timeline)
  const words = useEditor((s) => s.words)
  const edit = useEditor((s) => s.edit)
  const [anchor, setAnchor] = useState<number | null>(null)
  const [focus, setFocus] = useState<number | null>(null)
  const dragging = useRef(false)
  const listRef = useRef<HTMLDivElement>(null)

  const segments = timeline?.segments
  const { paras, removedCount } = useMemo(() => {
    if (!segments) return { paras: [] as Para[], removedCount: 0 }
    const map = programMap({ segments })
    const rows: Row[] = words.map((word, index) => ({ word, index, removed: isWordRemoved(map, word) }))
    const segOf = (w: SourceWord) => {
      const mid = (w.start + w.end) / 2
      const i = segments.findIndex((s) => s.source_id === w.source_id && mid >= s.start - 0.5 && mid <= s.end + 0.5)
      return i
    }
    const out: Para[] = []
    let cur: Row[] = []
    let lastSeg = -2
    let segLabel: Para['segment']
    const flush = () => {
      if (cur.length) out.push({ rows: cur, segment: segLabel })
      cur = []
      segLabel = undefined
    }
    rows.forEach((r, i) => {
      const seg = segOf(r.word)
      const prev = rows[i - 1]
      const gap = prev ? r.word.start - prev.word.end : 0
      const sentenceEnd = prev && /[.?!]$/.test(prev.word.text)
      if (seg !== lastSeg) {
        flush()
        const first = rows.slice(i).find((x) => !x.removed)
        const at = first ? sourceToProgram(map, first.word.source_id, first.word.start) : null
        segLabel = { index: Math.max(seg, 0), at: at ?? 0 }
        lastSeg = seg
      } else if (prev && (gap > 0.8 || (sentenceEnd && gap > 0.3))) flush()
      cur.push(r)
    })
    flush()
    return { paras: out, removedCount: rows.filter((r) => r.removed).length }
  }, [segments, words])

  const lo = anchor != null && focus != null ? Math.min(anchor, focus) : null
  const hi = anchor != null && focus != null ? Math.max(anchor, focus) : null
  const selected = lo != null && hi != null ? paras.flatMap((p) => p.rows).filter((r) => r.index >= lo && r.index <= hi) : []
  const selRemoved = selected.filter((r) => r.removed)
  const selKept = selected.filter((r) => !r.removed)

  const activeKey = useEngineValue(rt, (r) => {
    const t = r.engine.time
    const pw = r.getWords().words
    let key = ''
    for (const w of pw) {
      if (w.start <= t + 1e-6) key = w.key
      else break
    }
    return key
  })

  useEffect(() => {
    const up = () => (dragging.current = false)
    window.addEventListener('mouseup', up)
    return () => window.removeEventListener('mouseup', up)
  }, [])

  // keep the spoken word visible while playing
  useEffect(() => {
    if (!activeKey) return
    const el = listRef.current?.querySelector<HTMLElement>(`[data-key="${CSS.escape(activeKey)}"]`)
    const box = listRef.current
    if (!el || !box) return
    const r = el.getBoundingClientRect()
    const b = box.getBoundingClientRect()
    if (r.top < b.top + 30 || r.bottom > b.bottom - 30) el.scrollIntoView({ block: 'center', behavior: 'smooth' })
  }, [activeKey, rt])

  const seekTo = (w: SourceWord) => {
    if (!rt || !timeline) return
    const p = sourceToProgram(programMap(timeline), w.source_id, (w.start + w.end) / 2)
    if (p != null) rt.engine.seek(p)
  }

  const runs = (rows: Row[]) => {
    const out: Row[][] = []
    for (const r of rows) {
      const last = out[out.length - 1]
      if (last && last[last.length - 1].index === r.index - 1 && last[0].word.source_id === r.word.source_id) last.push(r)
      else out.push([r])
    }
    return out
  }

  const cut = () => {
    const rs = runs(selKept)
    if (!rs.length) return
    edit((tl) => rs.reduce((acc, run) => addCut(acc, run[0].word.source_id, run[0].word.start, run[run.length - 1].word.end, 'manual'), tl))
    setAnchor(null)
    setFocus(null)
  }
  const restore = () => {
    const rs = runs(selRemoved)
    if (!rs.length) return
    edit((tl) => rs.reduce((acc, run) => restoreRange(acc, run[0].word.source_id, run[0].word.start, run[run.length - 1].word.end), tl))
  }
  const emphasise = () => edit((tl) => selKept.reduce((acc, r) => toggleEmphasis(acc, r.word.key), tl))
  const hideCaps = () => edit((tl) => selKept.reduce((acc, r) => toggleHiddenWord(acc, r.word.key), tl))

  const onKey = (e: React.KeyboardEvent) => {
    if ((e.key === 'Delete' || e.key === 'Backspace') && selKept.length) {
      e.preventDefault()
      cut()
    } else if (e.key === 'Escape') {
      setAnchor(null)
      setFocus(null)
    }
  }

  const emph = new Set(timeline?.captions.emphasis ?? [])
  const hid = new Set(timeline?.captions.hidden ?? [])

  return (
    <aside className="panel panel-left" aria-label="Transcript">
      <header className="panel-head">
        <h2>Transcript</h2>
        {removedCount > 0 && <span className="muted small">{removedCount} word{removedCount === 1 ? '' : 's'} cut</span>}
      </header>
      <div className={cx('sel-bar', selected.length > 0 && 'is-open')} aria-hidden={selected.length === 0}>
        <span className="small">{selected.length} selected</span>
        <Button size="sm" kind="danger" icon="scissors" disabled={!selKept.length} onClick={cut} tabIndex={selected.length ? 0 : -1}>
          Cut
        </Button>
        <Button size="sm" icon="restore" disabled={!selRemoved.length} onClick={restore} tabIndex={selected.length ? 0 : -1}>
          Restore
        </Button>
        <Button size="sm" icon="emphasis" disabled={!selKept.length} onClick={emphasise} tabIndex={selected.length ? 0 : -1} title="Highlight these words in the captions">
          Emphasise
        </Button>
        <Button size="sm" icon="eyeoff" disabled={!selKept.length} onClick={hideCaps} tabIndex={selected.length ? 0 : -1} title="Hide these words from the captions only">
          Hide
        </Button>
      </div>
      <div className="transcript" ref={listRef} onKeyDown={onKey} tabIndex={-1}>
        {paras.length === 0 && <p className="muted pad">This clip has no transcript.</p>}
        {paras.map((p, pi) => (
          <div key={pi} className="para-wrap">
            {p.segment && (
              <div className="seg-label">
                Part {p.segment.index + 1}
                <span className="muted"> · at {formatTime(p.segment.at)}</span>
              </div>
            )}
            <p className="para">
              {p.rows.map((r) => {
                const sel = lo != null && hi != null && r.index >= lo && r.index <= hi
                return (
                  <span key={r.word.key}>
                    <button
                      type="button"
                      data-key={r.word.key}
                      className={cx(
                        'w',
                        r.removed && 'is-removed',
                        sel && 'is-sel',
                        activeKey === r.word.key && !r.removed && 'is-active',
                        emph.has(r.word.key) && 'is-emph',
                        hid.has(r.word.key) && 'is-hidden',
                        FILLERS.test(r.word.text.replace(/[^\w]/g, '')) && 'is-filler',
                      )}
                      title={r.removed ? `Cut. Select and press Restore to bring it back.` : `${formatTime(r.word.start, true)} in the source`}
                      onMouseDown={(e) => {
                        if (e.shiftKey && anchor != null) {
                          setFocus(r.index)
                        } else {
                          dragging.current = true
                          setAnchor(r.index)
                          setFocus(r.index)
                        }
                      }}
                      onMouseEnter={() => dragging.current && setFocus(r.index)}
                      onClick={() => {
                        if (!r.removed) seekTo(r.word)
                      }}
                      onDoubleClick={() => {
                        if (!r.removed) rt?.engine.play()
                      }}
                    >
                      {r.word.text}
                    </button>{' '}
                  </span>
                )
              })}
            </p>
          </div>
        ))}
      </div>
      <footer className="panel-foot muted small">
        <Icon name="scissors" size={13} /> Select words and press Delete to cut them. Click a word to jump there.
      </footer>
    </aside>
  )
}
