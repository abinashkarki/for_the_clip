import { useRef, useState } from 'react'
import { api, brandAssetUrl } from '../../api/client'
import type { LogoAnchor } from '../../render/types'
import { useEditor } from '../../state/editorStore'
import { useBrandKits } from '../../state/registry'
import { href } from '../../state/route'
import { Button, ColorField, Segmented, Select, Slider, Switch, cx, toast } from '../../ui/kit'
import { getLogo, getProgressBar, setLogo, setProgressBar } from '../ops'

const ANCHORS: { id: LogoAnchor; label: string }[] = [
  { id: 'top_left', label: 'Top left' },
  { id: 'top_right', label: 'Top right' },
  { id: 'bottom_left', label: 'Bottom left' },
  { id: 'bottom_right', label: 'Bottom right' },
]

export function BrandPanel() {
  const tl = useEditor((s) => s.timeline)
  const edit = useEditor((s) => s.edit)
  const busy = useEditor((s) => s.busyAction)
  const runAction = useEditor((s) => s.runAction)
  const { kits } = useBrandKits()
  const [kit, setKit] = useState('')
  const [uploading, setUploading] = useState(false)
  const file = useRef<HTMLInputElement>(null)
  if (!tl) return null
  const bar = getProgressBar(tl)
  const logo = getLogo(tl)
  const applied = tl.brand_kit_id ? kits.find((k) => k.id === tl.brand_kit_id) : null

  const upload = async (f: File | undefined) => {
    if (!f) return
    setUploading(true)
    try {
      const res = await api.uploadBrandAsset(f)
      edit((t) => setLogo(t, { asset_id: res.asset_id }))
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Could not upload that logo', 'bad')
    }
    setUploading(false)
  }

  return (
    <div className="panel-body">
      <h3 className="sub tight">Brand kit</h3>
      <p className="muted small">
        {applied ? `Using ${applied.name}.` : 'A kit sets captions, hook style, progress bar and logo in one step.'}
      </p>
      <div className="kit-apply">
        <Select label="Kit" value={kit} onChange={setKit} options={[{ value: '', label: kits.length ? 'Choose a kit' : 'No kits yet' }, ...kits.map((k) => ({ value: k.id, label: k.name }))]} />
        <Button kind="primary" icon="brand" disabled={!kit || !!busy} busy={busy === 'apply-brand-kit'} onClick={() => void runAction('apply-brand-kit', { brand_kit_id: kit })}>
          Apply kit
        </Button>
      </div>
      <a className="mini-link" href={href.brands()}>Manage brand kits</a>

      <h3 className="sub">Progress bar</h3>
      <Switch checked={!!bar} onChange={(on) => edit((t) => setProgressBar(t, on ? {} : null))} label="Show a progress bar" />
      {bar && (
        <div className="stack">
          <ColorField label="Colour" value={bar.color} onChange={(c) => edit((t) => setProgressBar(t, { color: c }), { coalesce: 'bar-color' })} />
          <Segmented label="Bar position" value={bar.position} onChange={(p) => edit((t) => setProgressBar(t, { position: p }))} options={[{ value: 'top', label: 'Top' }, { value: 'bottom', label: 'Bottom' }]} />
          <Slider label="Thickness" value={bar.thickness} min={4} max={40} suffix=" px" onChange={(v) => edit((t) => setProgressBar(t, { thickness: v }), { coalesce: 'bar-th' })} />
        </div>
      )}

      <h3 className="sub">Logo</h3>
      <div className="logo-row">
        <div className="logo-box">{logo ? <img src={brandAssetUrl(logo.asset_id)} alt="Current logo" /> : <span className="muted small">No logo</span>}</div>
        <div className="btn-col">
          <Button size="sm" icon="upload" busy={uploading} onClick={() => file.current?.click()}>
            {logo ? 'Replace logo' : 'Upload logo'}
          </Button>
          {logo && (
            <Button size="sm" kind="ghost" icon="trash" onClick={() => edit((t) => setLogo(t, null))}>
              Remove
            </Button>
          )}
          <input ref={file} type="file" accept="image/png,image/jpeg" hidden onChange={(e) => { void upload(e.target.files?.[0]); e.target.value = '' }} />
        </div>
      </div>
      <p className="muted small hint">PNG or JPEG, up to 5 MB.</p>
      {logo && (
        <div className="stack">
          <div className="anchor-grid" role="radiogroup" aria-label="Logo corner">
            {ANCHORS.map((a) => (
              <button key={a.id} role="radio" aria-checked={logo.anchor === a.id} className={cx('anchor', `a-${a.id}`, logo.anchor === a.id && 'is-on')} onClick={() => edit((t) => setLogo(t, { anchor: a.id }))} aria-label={a.label} title={a.label}>
                <i />
              </button>
            ))}
            <span className="muted small anchor-hint">Corner</span>
          </div>
          <Slider label="Size" value={Math.round(logo.scale * 100)} min={5} max={40} suffix="%" onChange={(v) => edit((t) => setLogo(t, { scale: v / 100 }), { coalesce: 'logo-s' })} />
          <Slider label="Opacity" value={Math.round(logo.opacity * 100)} min={10} max={100} suffix="%" onChange={(v) => edit((t) => setLogo(t, { opacity: v / 100 }), { coalesce: 'logo-o' })} />
        </div>
      )}
    </div>
  )
}
