import { type ButtonHTMLAttributes, type KeyboardEvent as ReactKeyboardEvent, type ReactNode, useEffect, useId, useRef, useState } from 'react'
import { create } from 'zustand'
import { Icon, type IconName } from './Icon'

export function cx(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(' ')
}

type BtnProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  kind?: 'primary' | 'secondary' | 'ghost' | 'danger'
  size?: 'sm' | 'md' | 'lg'
  icon?: IconName
  busy?: boolean
}
export function Button({ kind = 'secondary', size = 'md', icon, busy, children, className, disabled, ...rest }: BtnProps) {
  return (
    <button className={cx('btn', `btn-${kind}`, `btn-${size}`, className)} disabled={disabled || busy} {...rest}>
      {busy ? <Spinner size={14} /> : icon ? <Icon name={icon} size={size === 'lg' ? 18 : 16} /> : null}
      {children}
    </button>
  )
}

export function IconButton({
  icon,
  label,
  active,
  size = 18,
  className,
  ...rest
}: { icon: IconName; label: string; active?: boolean; size?: number } & ButtonHTMLAttributes<HTMLButtonElement>) {
  return (
    <button className={cx('icon-btn', active && 'is-active', className)} aria-label={label} title={label} {...rest}>
      <Icon name={icon} size={size} />
    </button>
  )
}

export function Spinner({ size = 16 }: { size?: number }) {
  return <span className="spinner" style={{ width: size, height: size }} role="status" aria-label="Working" />
}

export function ProgressBar({ value, label, tone }: { value: number; label?: string; tone?: 'accent' | 'ok' | 'bad' }) {
  const pct = Math.round(Math.min(Math.max(value, 0), 1) * 100)
  return (
    <div className="progress" role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100} aria-label={label}>
      <div className={cx('progress-fill', tone && `tone-${tone}`)} style={{ width: `${pct}%` }} />
    </div>
  )
}

export function Slider({
  label,
  value,
  min,
  max,
  step = 1,
  onChange,
  format,
  suffix,
}: {
  label: string
  value: number
  min: number
  max: number
  step?: number
  onChange: (v: number) => void
  format?: (v: number) => string
  suffix?: string
}) {
  const id = useId()
  return (
    <div className="field slider">
      <label htmlFor={id}>{label}</label>
      <input id={id} type="range" min={min} max={max} step={step} value={value} onChange={(e) => onChange(Number(e.target.value))} />
      <output htmlFor={id}>
        {format ? format(value) : Number.isInteger(step) ? value : value.toFixed(2)}
        {suffix}
      </output>
    </div>
  )
}

export function ColorField({
  label,
  value,
  onChange,
  onClear,
}: {
  label: string
  value: string | null
  onChange: (v: string) => void
  onClear?: () => void
}) {
  const id = useId()
  const hex = value && /^#[0-9a-f]{6}/i.test(value) ? value.slice(0, 7) : '#000000'
  return (
    <div className="field color-field">
      <label htmlFor={id}>{label}</label>
      <span className={cx('swatch', !value && 'is-empty')} style={value ? { background: value } : undefined}>
        <input id={id} type="color" value={hex} onChange={(e) => onChange(e.target.value.toUpperCase())} aria-label={label} />
      </span>
      <span className="color-hex">{value ? value.slice(0, 7).toUpperCase() : 'None'}</span>
      {onClear && value && (
        <button className="mini-link" onClick={onClear} type="button">
          Clear
        </button>
      )}
    </div>
  )
}

export function Segmented<T extends string>({
  value,
  options,
  onChange,
  label,
}: {
  value: T
  options: { value: T; label: ReactNode; title?: string }[]
  onChange: (v: T) => void
  label: string
}) {
  return (
    <div className="segmented" role="radiogroup" aria-label={label}>
      {options.map((o) => (
        <button
          key={o.value}
          role="radio"
          aria-checked={o.value === value}
          className={cx(o.value === value && 'is-on')}
          title={o.title}
          onClick={() => onChange(o.value)}
          type="button"
        >
          {o.label}
        </button>
      ))}
    </div>
  )
}

export function Select<T extends string>({
  label,
  value,
  options,
  onChange,
}: {
  label: string
  value: T
  options: { value: T; label: string }[]
  onChange: (v: T) => void
}) {
  const id = useId()
  return (
    <div className="field select-field">
      <label htmlFor={id}>{label}</label>
      <select id={id} value={value} onChange={(e) => onChange(e.target.value as T)}>
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
    </div>
  )
}

export function Switch({ checked, onChange, label }: { checked: boolean; onChange: (v: boolean) => void; label: string }) {
  return (
    <label className="switch">
      <input type="checkbox" role="switch" checked={checked} onChange={(e) => onChange(e.target.checked)} />
      <span className="switch-track" aria-hidden="true" />
      <span>{label}</span>
    </label>
  )
}

export function Dialog({
  open,
  onClose,
  title,
  children,
  footer,
  width = 520,
}: {
  open: boolean
  onClose: () => void
  title: string
  children: ReactNode
  footer?: ReactNode
  width?: number
}) {
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!open) return
    const prev = document.activeElement as HTMLElement | null
    ref.current?.querySelector<HTMLElement>('[data-autofocus], input, select, textarea, button')?.focus()
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.stopPropagation()
        onClose()
      }
      if (e.key === 'Tab' && ref.current) {
        const f = ref.current.querySelectorAll<HTMLElement>('button:not([disabled]), input:not([disabled]), select, textarea, a[href]')
        if (!f.length) return
        const first = f[0]
        const last = f[f.length - 1]
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault()
          last.focus()
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault()
          first.focus()
        }
      }
    }
    document.addEventListener('keydown', onKey, true)
    return () => {
      document.removeEventListener('keydown', onKey, true)
      prev?.focus?.()
    }
  }, [open, onClose])
  if (!open) return null
  return (
    <div className="scrim" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="dialog" role="dialog" aria-modal="true" aria-label={title} ref={ref} style={{ width }}>
        <header className="dialog-head">
          <h2>{title}</h2>
          <IconButton icon="x" label="Close" onClick={onClose} />
        </header>
        <div className="dialog-body">{children}</div>
        {footer && <footer className="dialog-foot">{footer}</footer>}
      </div>
    </div>
  )
}

// ---- toasts
interface Toast {
  id: number
  text: string
  tone: 'info' | 'bad' | 'ok'
  action?: ToastAction
}
export interface ToastAction {
  label: string
  onClick: () => void
}
export const useToasts = create<{ items: Toast[]; push: (t: string, tone?: Toast['tone'], action?: ToastAction) => void; dismiss: (id: number) => void }>((set) => ({
  items: [],
  push: (text, tone = 'info', action) => {
    const id = Date.now() + Math.random()
    set((s) => ({ items: [...s.items, { id, text, tone, action }].slice(-4) }))
    setTimeout(() => set((s) => ({ items: s.items.filter((t) => t.id !== id) })), action ? 9000 : tone === 'bad' ? 7000 : 3600)
  },
  dismiss: (id) => set((s) => ({ items: s.items.filter((t) => t.id !== id) })),
}))
export const toast = (text: string, tone?: Toast['tone'], action?: ToastAction) => useToasts.getState().push(text, tone, action)

export function Toasts() {
  const items = useToasts((s) => s.items)
  const dismiss = useToasts((s) => s.dismiss)
  return (
    <div className="toasts" role="status" aria-live="polite">
      {items.map((t) => (
        <div key={t.id} className={cx('toast', `toast-${t.tone}`)}>
          <Icon name={t.tone === 'bad' ? 'warn' : t.tone === 'ok' ? 'check' : 'wand'} size={16} />
          <span>{t.text}</span>
          {t.action && (
            <button className="toast-action" type="button" onClick={() => { t.action!.onClick(); dismiss(t.id) }}>
              {t.action.label}
            </button>
          )}
        </div>
      ))}
    </div>
  )
}

export function ScoreBadge({ score }: { score: number | null }) {
  if (score == null) return <span className="score score-na">n/a</span>
  const pct = Math.round(score * 100)
  const tone = pct >= 80 ? 'hi' : pct >= 60 ? 'mid' : 'lo'
  return (
    <span className={cx('score', `score-${tone}`)} title={`Score ${pct} out of 100`}>
      {pct}
    </span>
  )
}

export function EmptyState({ title, children, action }: { title: string; children?: ReactNode; action?: ReactNode }) {
  return (
    <div className="empty">
      <h3>{title}</h3>
      {children && <p>{children}</p>}
      {action}
    </div>
  )
}

// ---- overflow menu
export interface MenuItem {
  label: string
  onSelect: () => void
  danger?: boolean
  disabled?: boolean
  /** small right-aligned text, e.g. a size */
  hint?: string
}
export function Menu({ label, items, className }: { label: string; items: MenuItem[]; className?: string }) {
  const [open, setOpen] = useState(false)
  const root = useRef<HTMLDivElement>(null)
  const trigger = useRef<HTMLButtonElement>(null)
  const id = useId()
  useEffect(() => {
    if (!open) return
    root.current?.querySelector<HTMLElement>('[role="menuitem"]:not([disabled])')?.focus()
    const onDown = (e: MouseEvent) => {
      if (!root.current?.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    return () => document.removeEventListener('mousedown', onDown)
  }, [open])
  const close = (refocus: boolean) => {
    setOpen(false)
    if (refocus) trigger.current?.focus()
  }
  const onKey = (e: ReactKeyboardEvent) => {
    if (e.key === 'Escape') {
      e.stopPropagation()
      close(true)
      return
    }
    if (e.key === 'Tab') {
      setOpen(false)
      return
    }
    if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(e.key)) return
    const els = Array.from(root.current?.querySelectorAll<HTMLElement>('[role="menuitem"]:not([disabled])') ?? [])
    if (!els.length) return
    e.preventDefault()
    const i = els.indexOf(document.activeElement as HTMLElement)
    const next = e.key === 'Home' ? 0 : e.key === 'End' ? els.length - 1 : e.key === 'ArrowDown' ? (i + 1) % els.length : (i - 1 + els.length) % els.length
    els[next].focus()
  }
  return (
    <div className={cx('menu', className)} ref={root} onKeyDown={onKey}>
      <button
        ref={trigger}
        type="button"
        className="icon-btn"
        aria-label={label}
        title={label}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? id : undefined}
        onClick={() => setOpen((o) => !o)}
      >
        <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
          <circle cx="12" cy="5" r="1.7" />
          <circle cx="12" cy="12" r="1.7" />
          <circle cx="12" cy="19" r="1.7" />
        </svg>
      </button>
      {open && (
        <div className="menu-list" role="menu" id={id} aria-label={label}>
          {items.map((it) => (
            <button
              key={it.label}
              type="button"
              role="menuitem"
              className={cx('menu-item', it.danger && 'is-danger')}
              disabled={it.disabled}
              onClick={() => {
                close(true)
                it.onSelect()
              }}
            >
              <span>{it.label}</span>
              {it.hint && <span className="menu-hint">{it.hint}</span>}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
