import { create } from 'zustand'
import { api } from '../api/client'
import type { ClipRange, SourceQuality } from '../api/types'
import { toast } from '../ui/kit'

export interface LocalUpload {
  id: string
  name: string
  progress: number
  state: 'uploading' | 'sending' | 'error'
  error?: string
  sourceId?: string
  abort?: () => void
}

interface UploadStore {
  items: LocalUpload[]
  /** called with the new source id once the server accepted it */
  onAccepted: ((sourceId: string) => void) | null
  start: (file: File) => void
  addUrl: (url: string, opts?: { quality?: SourceQuality; range?: ClipRange }) => Promise<void>
  dismiss: (id: string) => void
  settle: (id: string) => void
}

let n = 0
export const useUploads = create<UploadStore>((set, get) => ({
  items: [],
  onAccepted: null,
  start: (file) => {
    if (!file.type.startsWith('video/') && !/\.(mp4|mov|mkv|webm|m4v|avi)$/i.test(file.name)) {
      toast('That file is not a video. Choose an MP4, MOV, MKV or WebM file.', 'bad')
      return
    }
    const id = `up-${++n}`
    const handle = api.upload(file, undefined, (f) =>
      set((s) => ({ items: s.items.map((i) => (i.id === id ? { ...i, progress: f, state: f >= 1 ? 'sending' : 'uploading' } : i)) })),
    )
    set((s) => ({ items: [...s.items, { id, name: file.name, progress: 0, state: 'uploading', abort: handle.abort }] }))
    handle.promise.then(
      (res) => {
        set((s) => ({ items: s.items.filter((i) => i.id !== id) }))
        get().onAccepted?.(res.source_id)
      },
      (e: Error) => set((s) => ({ items: s.items.map((i) => (i.id === id ? { ...i, state: 'error', error: e.message } : i)) })),
    )
  },
  addUrl: async (url, opts) => {
    try {
      const res = await api.addSource({ url, ...opts })
      get().onAccepted?.(res.source_id)
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Could not add that link', 'bad')
    }
  },
  dismiss: (id) => {
    get().items.find((i) => i.id === id)?.abort?.()
    set((s) => ({ items: s.items.filter((i) => i.id !== id) }))
  },
  settle: (id) => set((s) => ({ items: s.items.filter((i) => i.id !== id) })),
}))
