import { create } from 'zustand'
import { ApiError, api, newRequestId } from '../api/client'
import type { ActionResponse, ClipDetail, SaveResult } from '../api/types'
import { normalizeTimeline } from '../render/timeline'
import type { SourceWord, Timeline } from '../render/types'
import { toast } from '../ui/kit'

export type SaveState = 'idle' | 'dirty' | 'saving' | 'saved' | 'error' | 'conflict'

interface EditorState {
  clipId: string | null
  loading: boolean
  loadError: string | null
  detail: ClipDetail | null
  timeline: Timeline | null
  /** every source word the clip knows about (cut words included), in source order of segments */
  words: SourceWord[]
  revisionId: string
  past: Timeline[]
  future: Timeline[]
  /** bumps on every local change; used to tell whether a save response is still current */
  version: number
  save: SaveState
  saveError: string | null
  busyAction: string | null

  load: (id: string) => Promise<void>
  reset: () => void
  edit: (fn: (t: Timeline) => Timeline, opts?: { coalesce?: string }) => void
  undo: () => void
  redo: () => void
  flushSave: () => Promise<boolean>
  resolveConflict: (mode: 'reload' | 'keep') => Promise<void>
  runAction: (name: 'remove-fillers' | 'auto-reframe' | 'apply-brand-kit', extra?: Record<string, unknown>) => Promise<boolean>
  replaceFromServer: (res: SaveResult) => void
}

const HISTORY_LIMIT = 100
const SAVE_DELAY = 800

let saveTimer: ReturnType<typeof setTimeout> | undefined
let inflight: Promise<boolean> | null = null
let lastCoalesce: { key: string; at: number } | null = null
let pendingRequestId: { version: number; id: string } | null = null

export const useEditor = create<EditorState>((set, get) => {
  const schedule = () => {
    if (saveTimer) clearTimeout(saveTimer)
    saveTimer = setTimeout(() => void get().flushSave(), SAVE_DELAY)
  }

  return {
    clipId: null,
    loading: false,
    loadError: null,
    detail: null,
    timeline: null,
    words: [],
    revisionId: '',
    past: [],
    future: [],
    version: 0,
    save: 'idle',
    saveError: null,
    busyAction: null,

    reset: () => {
      if (saveTimer) clearTimeout(saveTimer)
      lastCoalesce = null
      pendingRequestId = null
      set({ clipId: null, detail: null, timeline: null, words: [], past: [], future: [], version: 0, save: 'idle', saveError: null, loadError: null, busyAction: null })
    },

    load: async (id) => {
      get().reset()
      set({ clipId: id, loading: true })
      try {
        const detail = await api.getClip(id)
        if (get().clipId !== id) return
        set({
          loading: false,
          detail,
          timeline: detail.timeline,
          words: detail.words.map(({ key, source_id, word_id, text, start, end }) => ({ key, source_id, word_id, text, start, end })),
          revisionId: detail.revision_id,
        })
      } catch (e) {
        if (get().clipId !== id) return
        set({ loading: false, loadError: e instanceof Error ? e.message : 'Could not load this clip' })
      }
    },

    edit: (fn, opts) => {
      const s = get()
      if (!s.timeline || s.save === 'conflict') return
      const next = fn(s.timeline)
      if (next === s.timeline) return
      const now = Date.now()
      const coalesced = !!opts?.coalesce && lastCoalesce?.key === opts.coalesce && now - lastCoalesce.at < 900
      lastCoalesce = opts?.coalesce ? { key: opts.coalesce, at: now } : null
      set({
        timeline: next,
        past: coalesced ? s.past : [...s.past, s.timeline].slice(-HISTORY_LIMIT),
        future: [],
        version: s.version + 1,
        save: 'dirty',
      })
      schedule()
    },

    undo: () => {
      const s = get()
      if (!s.past.length || !s.timeline || s.save === 'conflict') return
      lastCoalesce = null
      set({ timeline: s.past[s.past.length - 1], past: s.past.slice(0, -1), future: [s.timeline, ...s.future], version: s.version + 1, save: 'dirty' })
      schedule()
    },

    redo: () => {
      const s = get()
      if (!s.future.length || !s.timeline || s.save === 'conflict') return
      lastCoalesce = null
      set({ timeline: s.future[0], future: s.future.slice(1), past: [...s.past, s.timeline], version: s.version + 1, save: 'dirty' })
      schedule()
    },

    flushSave: async () => {
      if (saveTimer) clearTimeout(saveTimer)
      if (inflight) {
        await inflight
      }
      const s0 = get()
      if (s0.save !== 'dirty' && s0.save !== 'error') return s0.save !== 'conflict'
      if (!s0.timeline || !s0.clipId) return true
      const sent = s0.version
      const clipId = s0.clipId
      if (!pendingRequestId || pendingRequestId.version !== sent) pendingRequestId = { version: sent, id: newRequestId() }
      set({ save: 'saving', saveError: null })
      inflight = (async () => {
        try {
          const res = await api.saveTimeline(clipId, { base_revision: s0.revisionId, timeline: s0.timeline!, request_id: pendingRequestId!.id })
          if (get().clipId !== clipId) return true
          const cur = get()
          if (cur.version === sent) {
            // Nothing changed since: adopt the canonical timeline without touching history.
            const same = JSON.stringify(res.timeline) === JSON.stringify(cur.timeline)
            set({ revisionId: res.revision_id, timeline: same ? cur.timeline : res.timeline, save: 'saved' })
          } else {
            set({ revisionId: res.revision_id })
            schedule()
          }
          pendingRequestId = null
          return true
        } catch (e) {
          if (get().clipId !== clipId) return false
          if (e instanceof ApiError && e.status === 409) {
            set({ save: 'conflict', saveError: e.message })
          } else {
            set({ save: 'error', saveError: e instanceof Error ? e.message : 'Could not save' })
            saveTimer = setTimeout(() => void get().flushSave(), 4000)
          }
          return false
        } finally {
          inflight = null
        }
      })()
      return inflight
    },

    resolveConflict: async (mode) => {
      const id = get().clipId
      if (!id) return
      try {
        const detail = await api.getClip(id)
        if (mode === 'reload') {
          lastCoalesce = null
          pendingRequestId = null
          set({
            detail,
            timeline: detail.timeline,
            words: detail.words.map(({ key, source_id, word_id, text, start, end }) => ({ key, source_id, word_id, text, start, end })),
            revisionId: detail.revision_id,
            past: [],
            future: [],
            version: get().version + 1,
            save: 'idle',
            saveError: null,
          })
        } else {
          pendingRequestId = null
          set({ revisionId: detail.revision_id, detail: { ...get().detail!, revisions: detail.revisions }, save: 'dirty', saveError: null })
          await get().flushSave()
        }
      } catch (e) {
        toast(e instanceof Error ? e.message : 'Could not refresh the clip', 'bad')
      }
    },

    replaceFromServer: (res) => {
      const s = get()
      const next = normalizeTimeline(res.timeline)
      set({
        revisionId: res.revision_id,
        timeline: next,
        past: s.timeline ? [...s.past, s.timeline].slice(-HISTORY_LIMIT) : s.past,
        future: [],
        version: s.version + 1,
        save: 'saved',
      })
    },

    runAction: async (name, extra) => {
      const s = get()
      if (!s.clipId || s.busyAction) return false
      set({ busyAction: name })
      try {
        const ok = await get().flushSave()
        if (!ok) throw new Error('Fix the save problem first, then try again.')
        const id = get().clipId!
        const call = async (): Promise<ActionResponse> => {
          const body = { base_revision: get().revisionId, request_id: newRequestId() }
          if (name === 'remove-fillers') return api.removeFillers(id, { ...body, silence_min: Number(extra?.silence_min ?? 0.6) })
          if (name === 'auto-reframe') return api.autoReframe(id, body)
          return api.applyBrandKit(id, { ...body, brand_kit_id: String(extra?.brand_kit_id) })
        }
        let res = await call()
        if ('job_id' in res) {
          const jobId = res.job_id
          for (let i = 0; i < 400; i++) {
            await new Promise((r) => setTimeout(r, 800))
            const j = await api.getJob(jobId)
            if (j.status === 'failed') throw new Error(j.error ?? 'The analysis failed')
            if (j.status === 'done') {
              const result = j.result as Partial<SaveResult> | null
              res = result?.timeline && result.revision_id ? (result as SaveResult) : await call()
              break
            }
          }
          if ('job_id' in res) throw new Error('The analysis took too long')
        }
        if (get().clipId !== id) return false
        get().replaceFromServer(res)
        return true
      } catch (e) {
        if (e instanceof ApiError && e.status === 409) set({ save: 'conflict', saveError: e.message })
        else toast(e instanceof Error ? e.message : 'That did not work', 'bad')
        return false
      } finally {
        set({ busyAction: null })
      }
    },
  }
})
