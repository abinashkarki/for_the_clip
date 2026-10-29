import { useEffect, useState } from 'react'
import { Icon, type IconName, Logo } from '../ui/Icon'
import { Button, Dialog, IconButton, Spinner, cx } from '../ui/kit'
import { PreviewStage, Transport } from '../editor/Preview'
import { RuntimeProvider, useRuntime } from '../editor/runtime'
import { TimelineStrip } from '../editor/TimelineStrip'
import { TranscriptPanel } from '../editor/Transcript'
import { AudioPanel } from '../editor/panels/Audio'
import { BrandPanel } from '../editor/panels/Brand'
import { CaptionsPanel } from '../editor/panels/Captions'
import { HookPanel } from '../editor/panels/Hook'
import { LayoutPanel } from '../editor/panels/Layout'
import { splitShot } from '../editor/ops'
import { useEditor } from '../state/editorStore'
import { useDocumentTitle } from '../state/hooks'
import { getLastProject, href } from '../state/route'
import { PublishDialog, type PublishItem } from '../editor/PublishDialog'
import { ExportDialog } from './ExportDialog'

type Tab = 'captions' | 'layout' | 'hook' | 'brand' | 'audio'
const TABS: { id: Tab; label: string; icon: IconName }[] = [
  { id: 'captions', label: 'Captions', icon: 'text' },
  { id: 'layout', label: 'Layout', icon: 'frame' },
  { id: 'hook', label: 'Hook', icon: 'hook' },
  { id: 'brand', label: 'Brand', icon: 'brand' },
  { id: 'audio', label: 'Cuts', icon: 'scissors' },
]

export function EditorScreen({ id }: { id: string }) {
  const load = useEditor((s) => s.load)
  const loading = useEditor((s) => s.loading)
  const error = useEditor((s) => s.loadError)
  const detail = useEditor((s) => s.detail)
  const timeline = useEditor((s) => s.timeline)
  useDocumentTitle(detail?.clip.title ?? 'Editor')

  useEffect(() => {
    void load(id)
    return () => {
      // Flush pending edits; the next load() resets the store.
      void useEditor.getState().flushSave()
    }
  }, [id, load])

  useEffect(() => {
    const warn = (e: BeforeUnloadEvent) => {
      const s = useEditor.getState().save
      if (s === 'dirty' || s === 'saving' || s === 'error' || s === 'conflict') {
        e.preventDefault()
        e.returnValue = ''
      }
    }
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [])

  if (error)
    return (
      <div className="page">
        <div className="empty">
          <h3>Could not open this clip</h3>
          <p>{error}</p>
          <a className="btn btn-secondary" href={getLastProject() ? href.project(getLastProject()!) : href.home()}>Go back</a>
        </div>
      </div>
    )
  if (loading || !detail || !timeline || detail.clip.id !== id)
    return (
      <div className="editor-loading">
        <Spinner size={22} /> Opening clip…
      </div>
    )
  return (
    <RuntimeProvider>
      <EditorLayout />
    </RuntimeProvider>
  )
}

function EditorLayout() {
  const rt = useRuntime()
  const detail = useEditor((s) => s.detail)!
  const [tab, setTab] = useState<Tab>('captions')
  const [exporting, setExporting] = useState(false)
  const [publishing, setPublishing] = useState(false)
  const [help, setHelp] = useState(false)

  // keyboard shortcuts
  useEffect(() => {
    const isTyping = (t: EventTarget | null) => {
      const el = t as HTMLElement | null
      if (!el) return false
      const tag = el.tagName
      if (tag === 'TEXTAREA' || tag === 'SELECT' || el.isContentEditable) return true
      if (tag === 'INPUT') {
        const type = (el as HTMLInputElement).type
        return !['range', 'checkbox', 'radio', 'color', 'button'].includes(type)
      }
      return false
    }
    const down = (e: KeyboardEvent) => {
      const s = useEditor.getState()
      const mod = e.metaKey || e.ctrlKey
      if (mod && e.key.toLowerCase() === 'z') {
        if (isTyping(e.target)) return
        e.preventDefault()
        if (e.shiftKey) s.redo()
        else s.undo()
        return
      }
      if (mod && e.key.toLowerCase() === 'y') {
        if (isTyping(e.target)) return
        e.preventDefault()
        s.redo()
        return
      }
      if (mod && e.key.toLowerCase() === 'e') {
        e.preventDefault()
        setExporting(true)
        return
      }
      if (mod || e.altKey || isTyping(e.target) || !rt) return
      const eng = rt.engine
      switch (e.key) {
        case ' ':
          e.preventDefault()
          eng.toggle()
          break
        case 'k':
        case 'K':
          eng.pause()
          break
        case 'l':
        case 'L':
          if (!eng.playing) eng.play()
          else eng.setRate(eng.playbackRate >= 2 ? 1 : eng.playbackRate + 0.5)
          break
        case 'j':
        case 'J':
          eng.seek(eng.time - 2)
          break
        case 'ArrowLeft':
          e.preventDefault()
          eng.seek(eng.time - (e.shiftKey ? 1 : 1 / 30))
          break
        case 'ArrowRight':
          e.preventDefault()
          eng.seek(eng.time + (e.shiftKey ? 1 : 1 / 30))
          break
        case 'Home':
          eng.seek(0)
          break
        case 'End':
          eng.seek(eng.duration)
          break
        case 's':
        case 'S':
          s.edit((t) => splitShot(t, eng.time))
          break
        case '?':
          setHelp(true)
          break
      }
    }
    const up = (e: KeyboardEvent) => {
      if (e.key === ' ' && !isTyping(e.target)) e.preventDefault()
    }
    window.addEventListener('keydown', down)
    window.addEventListener('keyup', up)
    return () => {
      window.removeEventListener('keydown', down)
      window.removeEventListener('keyup', up)
    }
  }, [rt])

  const publishItem = (): PublishItem => {
    const s = useEditor.getState()
    const hook = s.timeline?.overlays.find((o) => o.kind === 'hook')
    return {
      clipId: detail.clip.id,
      title: detail.clip.title,
      revisionId: s.revisionId,
      duration: detail.clip.duration,
      hook: hook && 'text' in hook ? hook.text : null,
      post: detail.clip.post ?? null,
      width: s.timeline?.canvas.width,
      height: s.timeline?.canvas.height,
    }
  }

  return (
    <div className="editor">
      <EditorBar onExport={() => setExporting(true)} onPublish={() => setPublishing(true)} onHelp={() => setHelp(true)} backHref={href.project(detail.project_id)} />
      <ConflictBanner />
      <TranscriptPanel />
      <main className="center" aria-label="Preview">
        <PreviewStage />
        <Transport />
      </main>
      <aside className="panel panel-right" aria-label="Edit panels">
        <div className="tabs" role="tablist" aria-label="Edit panels">
          {TABS.map((t) => (
            <button key={t.id} role="tab" id={`tab-${t.id}`} aria-selected={tab === t.id} aria-controls={`pane-${t.id}`} className={cx('tab', tab === t.id && 'is-on')} onClick={() => setTab(t.id)} onKeyDown={(e) => {
              const i = TABS.findIndex((x) => x.id === tab)
              if (e.key === 'ArrowRight') setTab(TABS[(i + 1) % TABS.length].id)
              if (e.key === 'ArrowLeft') setTab(TABS[(i - 1 + TABS.length) % TABS.length].id)
            }} tabIndex={tab === t.id ? 0 : -1}>
              <Icon name={t.icon} size={16} />
              <span>{t.label}</span>
            </button>
          ))}
        </div>
        <div className="tab-pane" role="tabpanel" id={`pane-${tab}`} aria-labelledby={`tab-${tab}`}>
          {tab === 'captions' && <CaptionsPanel />}
          {tab === 'layout' && <LayoutPanel />}
          {tab === 'hook' && <HookPanel />}
          {tab === 'brand' && <BrandPanel />}
          {tab === 'audio' && <AudioPanel />}
        </div>
      </aside>
      <TimelineStrip />
      {exporting && (
        <ExportDialog
          items={[{ clipId: detail.clip.id, title: detail.clip.title, revisionId: useEditor.getState().revisionId }]}
          beforeStart={async () => {
            const ok = await useEditor.getState().flushSave()
            if (!ok) throw new Error('Your edits are not saved yet. Fix the save problem and try again.')
            return [{ clipId: detail.clip.id, title: detail.clip.title, revisionId: useEditor.getState().revisionId }]
          }}
          onClose={() => setExporting(false)}
        />
      )}
      {publishing && (
        <PublishDialog
          items={[publishItem()]}
          beforeStart={async () => {
            const ok = await useEditor.getState().flushSave()
            if (!ok) throw new Error('Your edits are not saved yet. Fix the save problem and try again.')
            return [publishItem()]
          }}
          onClose={() => setPublishing(false)}
        />
      )}
      <Dialog open={help} onClose={() => setHelp(false)} title="Keyboard shortcuts" width={460}>
        <dl className="keys">
          {[
            ['Space', 'Play or pause'],
            ['K', 'Pause'],
            ['L', 'Play, press again for faster'],
            ['J', 'Back two seconds'],
            ['Left, Right', 'One frame (hold Shift for one second)'],
            ['Home, End', 'Start, end'],
            ['S', 'Split the shot at the playhead'],
            ['Delete', 'Cut the selected transcript words'],
            ['Ctrl or Cmd + Z', 'Undo'],
            ['Shift + Ctrl or Cmd + Z', 'Redo'],
            ['Ctrl or Cmd + E', 'Export'],
          ].map(([k, v]) => (
            <div key={k}>
              <dt><kbd>{k}</kbd></dt>
              <dd>{v}</dd>
            </div>
          ))}
        </dl>
      </Dialog>
    </div>
  )
}

function EditorBar({ onExport, onPublish, onHelp, backHref }: { onExport: () => void; onPublish: () => void; onHelp: () => void; backHref: string }) {
  const detail = useEditor((s) => s.detail)!
  const save = useEditor((s) => s.save)
  const saveError = useEditor((s) => s.saveError)
  const canUndo = useEditor((s) => s.past.length > 0)
  const canRedo = useEditor((s) => s.future.length > 0)
  const undo = useEditor((s) => s.undo)
  const redo = useEditor((s) => s.redo)
  const flush = useEditor((s) => s.flushSave)
  return (
    <header className="editor-bar">
      <a className="back-link" href={backHref} aria-label="Back to project">
        <Icon name="back" size={18} />
      </a>
      <a className="brand brand-sm" href={href.home()} aria-label="Clippie Studio library"><Logo size={20} /></a>
      <h1 className="editor-title" title={detail.clip.title}>{detail.clip.title}</h1>
      <div className={cx('save', `save-${save}`)} role="status" aria-live="polite">
        {save === 'saving' && <><Spinner size={12} /> Saving…</>}
        {save === 'dirty' && <>Unsaved changes</>}
        {(save === 'saved' || save === 'idle') && <><Icon name="check" size={14} /> {save === 'saved' ? 'Saved' : 'Up to date'}</>}
        {save === 'error' && (
          <button className="link-btn" onClick={() => void flush()} title={saveError ?? ''}>
            <Icon name="warn" size={14} /> Could not save. Retry
          </button>
        )}
        {save === 'conflict' && <><Icon name="warn" size={14} /> Changed elsewhere</>}
      </div>
      <div className="bar-actions">
        <IconButton icon="undo" label="Undo (Ctrl+Z)" disabled={!canUndo} onClick={undo} />
        <IconButton icon="redo" label="Redo (Shift+Ctrl+Z)" disabled={!canRedo} onClick={redo} />
        <button className="mini-link kbd-link" onClick={onHelp}>Shortcuts</button>
        <Button icon="upload" onClick={onPublish}>
          Publish
        </Button>
        <Button kind="primary" icon="download" onClick={onExport}>
          Export
        </Button>
      </div>
    </header>
  )
}

function ConflictBanner() {
  const save = useEditor((s) => s.save)
  const resolve = useEditor((s) => s.resolveConflict)
  if (save !== 'conflict') return null
  return (
    <div className="conflict" role="alert">
      <Icon name="warn" size={18} />
      <div>
        <strong>This clip was changed somewhere else.</strong> Your edits are not saved yet. Autosave is paused.
      </div>
      <div className="btn-row">
        <Button size="sm" kind="primary" onClick={() => void resolve('keep')}>Keep my version</Button>
        <Button size="sm" onClick={() => void resolve('reload')}>Load the latest and drop my edits</Button>
      </div>
    </div>
  )
}
