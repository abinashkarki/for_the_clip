import { useEffect, useRef, useState } from 'react'
import { api, brandAssetUrl } from '../api/client'
import type { BrandKitInput } from '../api/types'
import { Compositor } from '../render/compose'
import { resolveTemplate } from '../render/captionLayout'
import { loadFont } from '../render/fonts'
import type { LogoOverlay, ProgramWord, Timeline } from '../render/types'
import { fontLabel, fontOptions, useBrandKits, useRegistry } from '../state/registry'
import { useDocumentTitle } from '../state/hooks'
import { href } from '../state/route'
import { Icon } from '../ui/Icon'
import { Button, ColorField, EmptyState, Segmented, Select, Slider, Switch, cx, toast } from '../ui/kit'
import { SAMPLE_WORDS } from '../editor/Thumbs'

const blank = (templateId: string): BrandKitInput => ({
  name: 'New brand kit',
  template_id: templateId,
  overrides: {},
  hook_style: 'hook_box',
  progress_bar: { enabled: true, color: '#FFE600' },
  logo_asset_id: null,
})

export function BrandsScreen() {
  useDocumentTitle('Brand kits')
  const reg = useRegistry()
  const { kits, reload, loading } = useBrandKits()
  const [sel, setSel] = useState<string | 'new' | null>(null)
  const [draft, setDraft] = useState<BrandKitInput | null>(null)
  const [saving, setSaving] = useState(false)
  const [dirty, setDirty] = useState(false)

  useEffect(() => {
    if (sel === null && kits.length && !draft) setSel(kits[0].id)
  }, [kits, sel, draft])

  useEffect(() => {
    if (!reg) return
    if (sel === 'new') {
      setDraft(blank(reg.captions[0]?.id ?? 'bold_pop'))
      setDirty(true)
    } else if (sel) {
      const k = kits.find((x) => x.id === sel)
      if (k) {
        const { id: _id, logo_url: _l, ...rest } = k
        void _id
        void _l
        setDraft(JSON.parse(JSON.stringify(rest)))
        setDirty(false)
      }
    }
  }, [sel, kits, reg])

  const patch = (p: Partial<BrandKitInput>) => {
    setDraft((d) => (d ? { ...d, ...p } : d))
    setDirty(true)
  }

  const save = async () => {
    if (!draft) return
    setSaving(true)
    try {
      const out = await api.saveBrandKit(draft, sel && sel !== 'new' ? sel : undefined)
      toast('Brand kit saved', 'ok')
      reload()
      setSel(out.id)
      setDirty(false)
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Could not save this kit', 'bad')
    }
    setSaving(false)
  }
  const remove = async () => {
    if (!sel || sel === 'new') return
    if (!confirm('Delete this brand kit? Clips that already use it keep their look.')) return
    try {
      await api.deleteBrandKit(sel)
      toast('Brand kit deleted', 'ok')
      setSel(null)
      setDraft(null)
      reload()
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Could not delete this kit', 'bad')
    }
  }
  const uploadLogo = async (f: File | undefined) => {
    if (!f) return
    try {
      const res = await api.uploadBrandAsset(f)
      patch({ logo_asset_id: res.asset_id })
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Could not upload that logo', 'bad')
    }
  }

  const base = reg?.captions.find((c) => c.id === draft?.template_id)
  const cur = base && draft ? resolveTemplate(base, draft.overrides as never) : null
  const ov = (path: string, v: unknown) => {
    if (!draft) return
    const o = JSON.parse(JSON.stringify(draft.overrides)) as Record<string, Record<string, unknown> | unknown>
    const [a, b] = path.split('.')
    if (b) {
      const bucket = (o[a] as Record<string, unknown>) ?? {}
      bucket[b] = v
      o[a] = bucket
    } else o[a] = v
    patch({ overrides: o })
  }
  const file = useRef<HTMLInputElement>(null)
  const fonts = fontOptions(reg)

  return (
    <div className="page">
      <nav className="crumbs"><a href={href.home()}><Icon name="back" size={15} /> Library</a></nav>
      <header className="project-head">
        <div>
          <h1 className="project-title">Brand kits</h1>
          <div className="muted">Save a caption style, hook, progress bar and logo once, then apply it to any clip.</div>
        </div>
        <Button kind="primary" icon="plus" onClick={() => setSel('new')}>New kit</Button>
      </header>

      <div className="brands">
        <ul className="kit-list" aria-label="Brand kits">
          {kits.map((k) => (
            <li key={k.id}>
              <button className={cx('kit-item', sel === k.id && 'is-on')} onClick={() => setSel(k.id)}>
                <span className="kit-logo">{k.logo_asset_id ? <img src={k.logo_url || brandAssetUrl(k.logo_asset_id)} alt="" /> : <Icon name="brand" size={16} />}</span>
                <span>
                  <span className="kit-name">{k.name}</span>
                  <span className="muted small">{reg?.captions.find((c) => c.id === k.template_id)?.name ?? k.template_id}</span>
                </span>
              </button>
            </li>
          ))}
          {sel === 'new' && (
            <li><span className="kit-item is-on"><span className="kit-logo"><Icon name="plus" size={16} /></span><span className="kit-name">New brand kit</span></span></li>
          )}
          {!loading && kits.length === 0 && sel !== 'new' && <li><EmptyState title="No brand kits yet">Create one to keep your clips consistent.</EmptyState></li>}
        </ul>

        {draft && cur && reg ? (
          <div className="kit-editor">
            <div className="kit-form">
              <label className="field">
                <span className="field-label">Name</span>
                <input className="input" value={draft.name} maxLength={60} onChange={(e) => patch({ name: e.target.value })} />
              </label>
              <Select label="Caption style" value={draft.template_id} onChange={(v) => patch({ template_id: v, overrides: {} })} options={reg.captions.map((c) => ({ value: c.id, label: c.name }))} />
              <div className="two">
                <Select label="Font" value={cur.font.file} onChange={(file) => {
                  const f = fonts.find((x) => x.file === file)!
                  const o = JSON.parse(JSON.stringify(draft.overrides))
                  o.font = { ...(o.font ?? {}), family: f.family, file: f.file }
                  patch({ overrides: o })
                }} options={fonts.map((f) => ({ value: f.file, label: fontLabel(f) }))} />
                <Slider label="Size" value={cur.font.size} min={24} max={160} onChange={(v) => { const o = JSON.parse(JSON.stringify(draft.overrides)); o.font = { ...(o.font ?? {}), size: v }; patch({ overrides: o }) }} />
              </div>
              <h3 className="sub">Colours</h3>
              <div className="stack">
                <ColorField label="Text" value={cur.colors.text} onChange={(v) => ov('colors.text', v)} />
                <ColorField label="Spoken word" value={cur.colors.active} onChange={(v) => ov('colors.active', v)} />
                <ColorField label="Emphasis" value={cur.colors.emphasis} onChange={(v) => ov('colors.emphasis', v)} />
                <ColorField label="Outline" value={cur.colors.stroke} onChange={(v) => ov('colors.stroke', v)} />
              </div>
              <h3 className="sub">Hook</h3>
              <Segmented label="Hook style" value={draft.hook_style} onChange={(v) => patch({ hook_style: v })} options={reg.hooks.map((h) => ({ value: h.id, label: h.name ?? h.id }))} />
              <h3 className="sub">Progress bar</h3>
              <Switch checked={draft.progress_bar.enabled} onChange={(v) => patch({ progress_bar: { ...draft.progress_bar, enabled: v } })} label="Show a progress bar" />
              {draft.progress_bar.enabled && <div className="stack"><ColorField label="Colour" value={draft.progress_bar.color} onChange={(v) => patch({ progress_bar: { ...draft.progress_bar, color: v } })} /></div>}
              <h3 className="sub">Logo</h3>
              <div className="logo-row">
                <div className="logo-box">{draft.logo_asset_id ? <img src={brandAssetUrl(draft.logo_asset_id)} alt="Logo" /> : <span className="muted small">No logo</span>}</div>
                <div className="btn-col">
                  <Button size="sm" icon="upload" onClick={() => file.current?.click()}>{draft.logo_asset_id ? 'Replace logo' : 'Upload logo'}</Button>
                  {draft.logo_asset_id && <Button size="sm" kind="ghost" icon="trash" onClick={() => patch({ logo_asset_id: null })}>Remove</Button>}
                  <input ref={file} type="file" accept="image/png,image/jpeg" hidden onChange={(e) => { void uploadLogo(e.target.files?.[0]); e.target.value = '' }} />
                </div>
              </div>
              <p className="muted small hint">PNG or JPEG, up to 5 MB.</p>
              <div className="kit-actions">
                <Button kind="primary" onClick={save} busy={saving} disabled={!dirty || !draft.name.trim()}>Save kit</Button>
                {sel && sel !== 'new' && <Button kind="danger" icon="trash" onClick={remove}>Delete</Button>}
              </div>
            </div>
            <BrandPreview kit={draft} />
          </div>
        ) : (
          !loading && sel !== 'new' && kits.length > 0 && <div className="muted pad">Choose a kit to edit it.</div>
        )}
      </div>
    </div>
  )
}

function BrandPreview({ kit }: { kit: BrandKitInput }) {
  const reg = useRegistry()
  const ref = useRef<HTMLCanvasElement>(null)
  const comp = useRef(new Compositor())
  const logos = useRef(new Map<string, HTMLImageElement>())
  const [tick, setTick] = useState(0)

  useEffect(() => {
    if (!kit.logo_asset_id || logos.current.has(kit.logo_asset_id)) return
    const img = new Image()
    img.onload = () => setTick((n) => n + 1)
    img.src = brandAssetUrl(kit.logo_asset_id)
    logos.current.set(kit.logo_asset_id, img)
  }, [kit.logo_asset_id])

  useEffect(() => {
    const canvas = ref.current
    if (!canvas || !reg) return
    const base = reg.captions.find((c) => c.id === kit.template_id)
    if (!base) return
    const tpl = resolveTemplate(base, kit.overrides as never)
    void loadFont(tpl.font.file).then(() => setTick((n) => n + 1))
    const hook = reg.hooks.find((h) => h.id === kit.hook_style)
    if (hook) void loadFont(hook.font.file).then(() => setTick((n) => n + 1))
    const ctx = canvas.getContext('2d')!
    const W = canvas.width
    const H = canvas.height
    // a gradient stands in for the picture
    const bg = document.createElement('canvas')
    bg.width = 640
    bg.height = 360
    const bctx = bg.getContext('2d')!
    const g = bctx.createLinearGradient(0, 0, 640, 360)
    g.addColorStop(0, '#26335c')
    g.addColorStop(1, '#6b4a78')
    bctx.fillStyle = g
    bctx.fillRect(0, 0, 640, 360)
    bctx.fillStyle = 'rgba(255,255,255,.28)'
    bctx.beginPath()
    bctx.arc(320, 190, 86, 0, Math.PI * 2)
    bctx.fill()
    const words: ProgramWord[] = SAMPLE_WORDS.map((w) => ({ ...w, start: w.start + 0.2, end: w.end + 0.2 }))
    const overlays: Timeline['overlays'] = [{ id: 'h', kind: 'hook', text: 'Your hook goes here', start: 0, end: 5, style: kit.hook_style }]
    if (kit.progress_bar.enabled) overlays.push({ id: 'p', kind: 'progress_bar', color: kit.progress_bar.color, position: 'bottom', thickness: 12 })
    if (kit.logo_asset_id) overlays.push({ id: 'l', kind: 'logo', asset_id: kit.logo_asset_id, anchor: 'top_right', scale: 0.14, opacity: 0.9 } as LogoOverlay)
    const timeline: Timeline = {
      schema_version: 3,
      canvas: { width: 1080, height: 1920, fps: 30, background: { kind: 'color', color: '#000' } },
      segments: [],
      shots: [{ id: 's', start: 0, end: 5, layout: 'fill', regions: [{ keyframes: [{ t: 0, x: 0.5, y: 0.5, zoom: 1 }] }] }],
      broll: [],
      captions: { enabled: true, template_id: kit.template_id, overrides: kit.overrides as never, emphasis: [], hidden: [] },
      overlays,
      brand_kit_id: null,
      audio: { normalize: true },
    }
    comp.current.draw(ctx, W, H, { timeline, t: 0.7, duration: 5, picture: { img: bg, src: { w: 640, h: 360 } }, registry: reg, words, boundaries: [], version: JSON.stringify(kit), logos: logos.current })
  }, [kit, reg, tick])

  return (
    <div className="kit-preview">
      <canvas ref={ref} width={405} height={720} aria-label="Brand kit preview" />
      <span className="muted small">Preview with sample words</span>
    </div>
  )
}
