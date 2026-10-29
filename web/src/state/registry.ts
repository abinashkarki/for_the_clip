import { useEffect, useState } from 'react'
import { api, isMock, onModeChange } from '../api/client'
import type { BrandKit, TemplateRegistry } from '../api/types'
import { configureFonts } from '../render/fonts'

let registryPromise: Promise<TemplateRegistry> | null = null
let cached: TemplateRegistry | null = null

onModeChange(() => {
  registryPromise = null
  cached = null
})

const titleCase = (id: string) => id.replace(/^hook_/, '').replace(/_/g, ' ').replace(/^./, (c) => c.toUpperCase())

/** Tolerate small shape differences in /templates (hook fonts as strings, sizes under font, missing names). */
export function normalizeRegistry(raw: TemplateRegistry): TemplateRegistry {
  const fam = (file: string) => file.replace(/\.[a-z0-9]+$/i, '').split('-')[0]
  const hooks = (raw.hooks ?? []).map((h) => {
    const any = h as unknown as Record<string, any>
    const font = typeof any.font === 'string' ? { family: fam(any.font), file: any.font } : { family: any.font?.family ?? fam(any.font?.file ?? 'Inter'), file: any.font?.file ?? 'Inter-Bold.ttf' }
    const pos = typeof any.position === 'number' ? { y: any.position } : { y: any.position?.y ?? 0.16 }
    return {
      ...h,
      name: any.name ?? titleCase(h.id),
      font: { ...font, metrics: any.font?.metrics },
      size: any.size ?? any.font?.size ?? 58,
      colors: { text: any.colors?.text ?? '#FFFFFF', box: any.colors?.box ?? null, stroke: any.colors?.stroke ?? null },
      stroke_width: any.stroke_width ?? 0,
      shadow: any.shadow ?? { dx: 0, dy: 0, blur: 0, color: '#00000000' },
      position: pos,
      max_width: any.max_width ?? any.position?.max_width ?? 0.86,
      padding: any.padding ?? 28,
      radius: any.radius ?? 0,
      case: any.case ?? 'none',
      line_height: any.line_height ?? 1.15,
      band: any.band ?? any.full_width ?? false,
      animation: any.animation ?? { in: 'none', duration_ms: 0 },
    }
  })
  const captions = (raw.captions ?? []).map((c) => ({ ...c, name: c.name ?? titleCase(c.id), line_height: c.line_height ?? 1.12 }))
  return { ...raw, captions, hooks, layouts: raw.layouts ?? ['fill', 'fit', 'stack', 'screen_speaker'] }
}

export function loadRegistry(): Promise<TemplateRegistry> {
  if (!registryPromise) {
    registryPromise = api.templates().then(normalizeRegistry).then((r) => {
      configureFonts({ baseUrl: r.fonts_base_url, mock: isMock() })
      cached = r
      return r
    })
    registryPromise.catch(() => (registryPromise = null))
  }
  return registryPromise
}

export function useRegistry(): TemplateRegistry | null {
  const [reg, setReg] = useState<TemplateRegistry | null>(cached)
  useEffect(() => {
    let alive = true
    loadRegistry().then((r) => alive && setReg(r)).catch(() => {})
    return () => {
      alive = false
    }
  }, [])
  return reg
}

export function fontOptions(reg: TemplateRegistry | null): { family: string; file: string }[] {
  if (!reg) return []
  const seen = new Map<string, { family: string; file: string }>()
  for (const c of reg.captions) if (!seen.has(c.font.file)) seen.set(c.font.file, { family: c.font.family, file: c.font.file })
  for (const h of reg.hooks) if (!seen.has(h.font.file)) seen.set(h.font.file, { family: h.font.family, file: h.font.file })
  return [...seen.values()]
}

export function fontLabel(f: { family: string; file: string }): string {
  const weight = f.file.replace(/\.[a-z0-9]+$/i, '').split('-')[1] ?? ''
  return weight && weight !== 'Regular' ? `${f.family} ${weight.replace(/([a-z])([A-Z])/g, '$1 $2')}` : f.family
}

export function useBrandKits(): { kits: BrandKit[]; reload: () => void; loading: boolean } {
  const [kits, setKits] = useState<BrandKit[]>([])
  const [loading, setLoading] = useState(true)
  const [n, setN] = useState(0)
  useEffect(() => {
    let alive = true
    api
      .listBrandKits()
      .then((k) => alive && (setKits(k), setLoading(false)))
      .catch(() => alive && setLoading(false))
    return () => {
      alive = false
    }
  }, [n])
  return { kits, reload: () => setN((x) => x + 1), loading }
}
