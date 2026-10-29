import { useCallback, useEffect, useRef, useState } from 'react'

export interface Async<T> {
  data: T | null
  error: string | null
  loading: boolean
  reload: () => void
}

/** Load and optionally poll. `intervalFor(data)` returns ms to re-poll, or 0 to stop. */
export function useAsync<T>(load: () => Promise<T>, deps: unknown[], intervalFor?: (d: T) => number): Async<T> {
  const [state, setState] = useState<{ data: T | null; error: string | null; loading: boolean }>({ data: null, error: null, loading: true })
  const loadRef = useRef(load)
  loadRef.current = load
  const intervalRef = useRef(intervalFor)
  intervalRef.current = intervalFor
  const [tick, setTick] = useState(0)
  const reload = useCallback(() => setTick((n) => n + 1), [])

  useEffect(() => {
    let alive = true
    let timer: ReturnType<typeof setTimeout> | undefined
    const run = async (first: boolean) => {
      if (first) setState((s) => ({ ...s, loading: true }))
      try {
        const data = await loadRef.current()
        if (!alive) return
        setState({ data, error: null, loading: false })
        const ms = intervalRef.current?.(data) ?? 0
        if (ms > 0) timer = setTimeout(() => run(false), ms)
      } catch (e) {
        if (!alive) return
        setState((s) => ({ data: s.data, error: e instanceof Error ? e.message : String(e), loading: false }))
        if (!first && intervalRef.current) timer = setTimeout(() => run(false), 4000)
      }
    }
    run(true)
    return () => {
      alive = false
      if (timer) clearTimeout(timer)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick])

  return { ...state, reload }
}

export function statusKind(status: string): 'ready' | 'running' | 'failed' {
  const s = status.toLowerCase()
  if (['ready', 'partially_ready', 'done', 'completed', 'complete', 'succeeded', 'success'].includes(s)) return 'ready'
  if (['failed', 'needs_attention', 'error', 'cancelled', 'canceled'].includes(s)) return 'failed'
  return 'running'
}

export function timeAgo(iso: string): string {
  const ms = Date.now() - new Date(iso).getTime()
  if (!isFinite(ms)) return ''
  const m = Math.round(ms / 60000)
  if (m < 1) return 'Just now'
  if (m < 60) return `${m} min ago`
  const h = Math.round(m / 60)
  if (h < 24) return `${h} h ago`
  const d = Math.round(h / 24)
  return `${d} day${d === 1 ? '' : 's'} ago`
}

export function useDocumentTitle(title: string) {
  useEffect(() => {
    const prev = document.title
    document.title = title ? `${title} · Clippie Studio` : 'Clippie Studio'
    return () => {
      document.title = prev
    }
  }, [title])
}
