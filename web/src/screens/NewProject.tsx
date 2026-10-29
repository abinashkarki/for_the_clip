import { useEffect, useState } from 'react'
import { api } from '../api/client'
import type { ProjectOptions, SourceSummary } from '../api/types'
import { useAsync } from '../state/hooks'
import { statusKind } from '../state/hooks'
import { navigate, href } from '../state/route'
import { useBrandKits, useRegistry } from '../state/registry'
import { SourceProgress } from '../ui/Intake'
import { Button, Dialog, Segmented, Select, toast } from '../ui/kit'

export function NewProjectDialog({ sourceId, onClose }: { sourceId: string; onClose: () => void }) {
  const reg = useRegistry()
  const { kits } = useBrandKits()
  const src = useAsync<SourceSummary | null>(
    async () => (await api.listSources()).find((s) => s.id === sourceId) ?? null,
    [sourceId],
    (s) => (s && statusKind(s.status) === 'running' ? 1500 : 0),
  )
  const [count, setCount] = useState<'auto' | '3' | '5' | '8'>('auto')
  const [maxDur, setMaxDur] = useState<'30' | '60' | '90'>('60')
  const [aspect, setAspect] = useState<ProjectOptions['aspect']>('9:16')
  const [template, setTemplate] = useState('')
  const [kit, setKit] = useState('')
  const [focus, setFocus] = useState('')
  const [busy, setBusy] = useState(false)
  const kind = src.data ? statusKind(src.data.status) : 'running'

  useEffect(() => {
    if (kit) {
      const k = kits.find((x) => x.id === kit)
      if (k) setTemplate(k.template_id)
    }
  }, [kit, kits])

  const make = async () => {
    setBusy(true)
    try {
      const options: ProjectOptions = {
        aspect,
        count: count === 'auto' ? 'auto' : Number(count),
        max_duration: Number(maxDur),
        ...(template ? { template_id: template } : {}),
        ...(kit ? { brand_kit_id: kit } : {}),
        ...(focus.trim() ? { focus: focus.trim() } : {}),
      }
      const p = await api.createProject({ source_ids: [sourceId], name: src.data?.title, options })
      onClose()
      navigate(href.project(p.id))
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Could not start the project', 'bad')
      setBusy(false)
    }
  }

  return (
    <Dialog
      open
      onClose={onClose}
      title="Make clips"
      width={560}
      footer={
        <>
          <Button kind="ghost" onClick={onClose}>
            Not now
          </Button>
          <Button kind="primary" icon="wand" onClick={make} busy={busy} disabled={kind !== 'ready'}>
            {kind === 'ready' ? 'Make clips' : 'Waiting for the video'}
          </Button>
        </>
      }
    >
      <div className="np-source">
        {src.data?.thumbnail_url ? <img src={src.data.thumbnail_url} alt="" /> : <div className="np-thumb-ph" />}
        <div>
          <div className="np-title">{src.data?.title ?? 'Loading…'}</div>
          {kind === 'running' && src.data && <SourceProgress source={src.data} />}
          {kind === 'ready' && <div className="muted small">Ready. Transcript done.</div>}
          {kind === 'failed' && <div className="bad small">{src.data?.error ?? 'This video could not be processed.'}</div>}
        </div>
      </div>

      <div className="form-grid">
        <div className="field">
          <span className="field-label">Clips</span>
          <Segmented
            label="Clip count"
            value={count}
            onChange={setCount}
            options={[
              { value: 'auto', label: 'Auto' },
              { value: '3', label: '3' },
              { value: '5', label: '5' },
              { value: '8', label: '8' },
            ]}
          />
        </div>
        <div className="field">
          <span className="field-label">Longest clip</span>
          <Segmented
            label="Maximum duration"
            value={maxDur}
            onChange={setMaxDur}
            options={[
              { value: '30', label: '30 s' },
              { value: '60', label: '60 s' },
              { value: '90', label: '90 s' },
            ]}
          />
        </div>
        <div className="field">
          <span className="field-label">Shape</span>
          <Segmented
            label="Aspect ratio"
            value={aspect}
            onChange={setAspect}
            options={[
              { value: '9:16', label: '9:16' },
              { value: '1:1', label: '1:1' },
              { value: '4:5', label: '4:5' },
              { value: '16:9', label: '16:9' },
            ]}
          />
        </div>
        <Select
          label="Brand kit"
          value={kit}
          onChange={setKit}
          options={[{ value: '', label: 'None' }, ...kits.map((k) => ({ value: k.id, label: k.name }))]}
        />
        <Select
          label="Caption style"
          value={template}
          onChange={setTemplate}
          options={[{ value: '', label: 'Let Clippie choose' }, ...(reg?.captions ?? []).map((c) => ({ value: c.id, label: c.name }))]}
        />
      </div>
      <div className="field">
        <label htmlFor="np-focus">What should the clips be about? <span className="muted">Optional</span></label>
        <input id="np-focus" className="input" value={focus} maxLength={400} onChange={(e) => setFocus(e.target.value)} placeholder="For example: moments where the speaker explains how it works" />
      </div>
    </Dialog>
  )
}
