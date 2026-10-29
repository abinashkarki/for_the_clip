// Per-clip playback runtime: the engine, a redraw bus, and logo images, shared by every canvas in the editor.
import { type ReactNode, createContext, useContext, useEffect, useMemo, useRef, useState } from 'react'
import { assetUrl, brandAssetUrl, isMock } from '../api/client'
import { PlaybackEngine, createVideoElement } from '../render/playback'
import { programMap, programWords } from '../render/timeline'
import type { LogoOverlay, ProgramWord, Timeline, SourceWord } from '../render/types'
import { useEditor } from '../state/editorStore'
import { useRegistry } from '../state/registry'
import { loadFont } from '../render/fonts'
import { resolveTemplate } from '../render/captionLayout'

type Listener = () => void
export class FrameBus {
  private subs = new Set<Listener>()
  private raf = 0
  subscribe(cb: Listener) {
    this.subs.add(cb)
    return () => {
      this.subs.delete(cb)
    }
  }
  request = () => {
    if (this.raf) return
    this.raf = requestAnimationFrame(() => {
      this.raf = 0
      this.subs.forEach((s) => s())
    })
  }
  dispose() {
    if (this.raf) cancelAnimationFrame(this.raf)
    this.subs.clear()
  }
}

export interface Runtime {
  engine: PlaybackEngine
  bus: FrameBus
  logos: Map<string, HTMLImageElement>
  /** programme words for the current timeline, memoised per timeline identity */
  getWords: () => { words: ProgramWord[]; boundaries: number[]; version: string }
}

const Ctx = createContext<Runtime | null>(null)
export const useRuntime = () => useContext(Ctx)

export function RuntimeProvider({ children }: { children: ReactNode }) {
  const detail = useEditor((s) => s.detail)
  const timeline = useEditor((s) => s.timeline)
  const registry = useRegistry()
  const [rt, setRt] = useState<Runtime | null>(null)
  const clipId = detail?.clip.id

  useEffect(() => {
    if (!detail) return
    const bus = new FrameBus()
    const logos = new Map<string, HTMLImageElement>()
    const engine = new PlaybackEngine({
      createMedia: (sid) => {
        const src = detail.sources[sid] ?? Object.values(detail.sources)[0]
        const v = createVideoElement(assetUrl(src.media_url))
        for (const ev of ['seeked', 'loadeddata', 'loadedmetadata', 'canplay']) v.addEventListener(ev, bus.request)
        return v
      },
    })
    engine.subscribe(bus.request)
    let cache: { tl: Timeline | null; words: SourceWord[] | null; out: { words: ProgramWord[]; boundaries: number[]; version: string } } = {
      tl: null,
      words: null,
      out: { words: [], boundaries: [], version: '0' },
    }
    let n = 0
    const getWords = () => {
      const s = useEditor.getState()
      if (s.timeline !== cache.tl || s.words !== cache.words) {
        cache = { tl: s.timeline, words: s.words, out: {
            words: s.timeline ? programWords(s.timeline, s.words) : [],
            boundaries: s.timeline ? programMap(s.timeline).map((r) => r.at) : [],
            version: String(++n),
          },
        }
      }
      return cache.out
    }
    setRt({ engine, bus, logos, getWords })
    if (isMock()) (window as unknown as { __clippie?: unknown }).__clippie = { engine, bus }
    const onFonts = () => bus.request()
    document.fonts?.addEventListener?.('loadingdone', onFonts)
    return () => {
      document.fonts?.removeEventListener?.('loadingdone', onFonts)
      engine.dispose()
      bus.dispose()
      setRt(null)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [clipId])

  const segments = timeline?.segments
  const ranges = useMemo(() => (segments ? programMap({ segments }) : []), [segments])
  useEffect(() => {
    if (rt) rt.engine.setRanges(ranges)
  }, [rt, ranges])

  // every timeline change repaints
  useEffect(() => {
    rt?.bus.request()
  }, [rt, timeline])

  // fonts
  const tplId = timeline?.captions.template_id
  const overrides = timeline?.captions.overrides
  useEffect(() => {
    if (!registry || !timeline || !rt) return
    const base = registry.captions.find((c) => c.id === tplId)
    if (base) void loadFont(resolveTemplate(base, overrides).font.file).then(rt.bus.request)
    for (const h of registry.hooks) void loadFont(h.font.file).then(rt.bus.request)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [registry, tplId, overrides, rt])

  // logos
  const logoIds = (timeline?.overlays.filter((o) => o.kind === 'logo') as LogoOverlay[] | undefined)?.map((o) => o.asset_id).join(',')
  useEffect(() => {
    if (!rt) return
    for (const id of (logoIds ?? '').split(',').filter(Boolean)) {
      if (rt.logos.has(id)) continue
      const img = new Image()
      img.crossOrigin = 'anonymous'
      img.onload = rt.bus.request
      img.src = brandAssetUrl(id)
      rt.logos.set(id, img)
    }
  }, [rt, logoIds])

  const ref = useRef(rt)
  ref.current = rt
  return <Ctx.Provider value={rt}>{children}</Ctx.Provider>
}

/** Re-render only when a derived value from the engine changes (e.g. the active word). */
export function useEngineValue<T>(rt: Runtime | null, pick: (rt: Runtime) => T, eq: (a: T, b: T) => boolean = Object.is): T | null {
  const [v, setV] = useState<T | null>(rt ? pick(rt) : null)
  const last = useRef<T | null>(v)
  useEffect(() => {
    if (!rt) return
    const run = () => {
      const next = pick(rt)
      if (last.current === null || !eq(last.current, next)) {
        last.current = next
        setV(next)
      }
    }
    run()
    return rt.engine.subscribe(run)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rt])
  return v
}
