import { useMemo } from 'react'
import { resolveTemplate } from '../../render/captionLayout'
import type { ActiveStyle, AnimationIn, CaseMode, CaptionColors } from '../../render/types'
import { useEditor } from '../../state/editorStore'
import { fontLabel, fontOptions, useRegistry } from '../../state/registry'
import { Button, ColorField, Select, Slider, Switch, cx } from '../../ui/kit'
import { setCaptionTemplate, setCaptionsEnabled, setFontOverride, setOverride } from '../ops'
import { CaptionThumb } from '../Thumbs'
import { useEngineValue, useRuntime } from '../runtime'

const keepAlpha = (old: string | null | undefined, next: string) => (old && /^#[0-9a-f]{8}$/i.test(old) ? next + old.slice(7) : next)

export function CaptionsPanel() {
  const rt = useRuntime()
  const reg = useRegistry()
  const tl = useEditor((s) => s.timeline)
  const edit = useEditor((s) => s.edit)
  const anchor = useEngineValue(rt, (r) => Math.floor(r.engine.time * 2) / 2) ?? 0
  const words = useMemo(() => (rt ? rt.getWords().words : []), [rt, tl])

  if (!tl || !reg) return <div className="panel-body pad muted">Loading templates…</div>
  const cap = tl.captions
  const base = reg.captions.find((c) => c.id === cap.template_id) ?? reg.captions[0]
  const cur = resolveTemplate(base, cap.overrides)
  const fonts = fontOptions(reg)
  const hasOverrides = Object.keys(cap.overrides).length > 0

  const set = (path: string, value: unknown, baseValue: unknown) =>
    edit((t) => setOverride(t, path, value === baseValue ? undefined : value), { coalesce: `cap-${path}` })
  const setColor = (k: keyof CaptionColors, v: string | null) => set(`colors.${k}`, v, base.colors[k])

  return (
    <div className="panel-body">
      <div className="row-between">
        <Switch checked={cap.enabled} onChange={(v) => edit((t) => setCaptionsEnabled(t, v))} label="Show captions" />
      </div>

      <h3 className="sub">Style</h3>
      <div className={cx('gallery', !cap.enabled && 'is-off')} role="listbox" aria-label="Caption styles">
        {reg.captions.map((c) => (
          <button
            key={c.id}
            role="option"
            aria-selected={c.id === cap.template_id}
            className={cx('gal-item', c.id === cap.template_id && 'is-on')}
            onClick={() => edit((t) => setCaptionTemplate(setCaptionsEnabled(t, true), c.id))}
          >
            <CaptionThumb template={c.id === cap.template_id ? cur : c} words={words} anchorTime={anchor} />
            <span className="gal-name">{c.name}</span>
          </button>
        ))}
      </div>

      <h3 className="sub row-between">
        <span>Adjust</span>
        {hasOverrides && (
          <button className="mini-link" onClick={() => edit((t) => setCaptionTemplate(t, t.captions.template_id))}>
            Reset to style
          </button>
        )}
      </h3>
      <div className="stack">
        <Select
          label="Font"
          value={cur.font.file}
          onChange={(file) => {
            const f = fonts.find((x) => x.file === file)!
            edit((t) => (file === base.font.file ? setOverride(setOverride(t, 'font.family', undefined), 'font.file', undefined) : setFontOverride(t, f.family, f.file)))
          }}
          options={fonts.map((f) => ({ value: f.file, label: fontLabel(f) }))}
        />
        <Slider label="Size" value={cur.font.size} min={24} max={160} step={1} onChange={(v) => set('font.size', v, base.font.size)} />
        <Slider label="Position" value={Math.round(cur.position.y * 100)} min={10} max={95} step={1} suffix="%" onChange={(v) => set('position.y', v / 100, base.position.y)} />
        <Slider label="Words per line" value={cur.grouping.max_words} min={1} max={8} step={1} onChange={(v) => set('grouping.max_words', v, base.grouping.max_words)} />
        <Slider label="Outline" value={cur.stroke_width} min={0} max={20} step={1} onChange={(v) => set('stroke_width', v, base.stroke_width)} />
        <div className="two">
          <Select<CaseMode> label="Letters" value={cur.case} onChange={(v) => set('case', v, base.case)} options={[{ value: 'none', label: 'As spoken' }, { value: 'upper', label: 'UPPERCASE' }, { value: 'lower', label: 'lowercase' }]} />
          <Select<AnimationIn> label="Entrance" value={cur.animation.in} onChange={(v) => set('animation.in', v, base.animation.in)} options={[{ value: 'none', label: 'None' }, { value: 'fade', label: 'Fade' }, { value: 'pop', label: 'Pop' }, { value: 'slide_up', label: 'Slide up' }]} />
        </div>
        <Select<ActiveStyle>
          label="Spoken word"
          value={cur.active_word.style}
          onChange={(v) => set('active_word.style', v, base.active_word.style)}
          options={[
            { value: 'none', label: 'No highlight' },
            { value: 'color', label: 'Colour' },
            { value: 'box', label: 'Box' },
            { value: 'color_box', label: 'Colour and box' },
            { value: 'scale', label: 'Grow' },
          ]}
        />
      </div>

      <h3 className="sub">Colours</h3>
      <div className="stack">
        <ColorField label="Text" value={cur.colors.text} onChange={(v) => setColor('text', keepAlpha(cur.colors.text, v))} />
        <ColorField label="Spoken word" value={cur.colors.active} onChange={(v) => setColor('active', v)} />
        <ColorField label="Emphasis" value={cur.colors.emphasis} onChange={(v) => setColor('emphasis', v)} />
        <ColorField label="Outline" value={cur.colors.stroke} onChange={(v) => setColor('stroke', v)} />
        <ColorField label="Caption box" value={cur.colors.box} onChange={(v) => setColor('box', keepAlpha(cur.colors.box, v))} onClear={() => setColor('box', null)} />
        {(cur.active_word.style === 'box' || cur.active_word.style === 'color_box') && (
          <ColorField label="Word box" value={cur.colors.active_box ?? cur.colors.active} onChange={(v) => setColor('active_box', v)} />
        )}
      </div>
      <p className="muted small hint">
        Select words in the transcript and choose Emphasise to draw them in the emphasis colour.
      </p>
      {!reg.captions.length && <Button disabled>No templates</Button>}
    </div>
  )
}
