import { create } from 'zustand'
import { api } from '../api/client'
import type { ActivityItem, ActivityResponse } from '../api/types'
import { needsPolling } from './intake'

interface ActivityStore {
  data: ActivityResponse | null
  error: string | null
  open: boolean
  setOpen: (open: boolean) => void
  refresh: () => Promise<void>
  act: (item: ActivityItem, action: 'cancel' | 'retry' | 'resume') => Promise<string | null>
}

export const useActivity = create<ActivityStore>((set, get) => ({
  data: null,
  error: null,
  open: false,
  setOpen: (open) => set({ open }),
  refresh: async () => {
    try {
      set({ data: await api.getActivity(), error: null })
    } catch (e) {
      set({ error: e instanceof Error ? e.message : 'Could not load activity' })
    }
  },
  act: async (item, action) => {
    try {
      if (item.kind === 'source' && action === 'cancel') await api.cancelSource(item.id)
      else if (item.kind === 'source' && action === 'retry') await api.retrySource(item.id)
      else if (item.kind === 'project' && action === 'resume') await api.resumeProject(item.id)
      await get().refresh()
      return null
    } catch (e) {
      await get().refresh()
      return e instanceof Error ? e.message : 'That did not work'
    }
  },
}))

/** Poll while anything runs (or the panel is open), slowly otherwise. Returns a stop function. */
export function startActivityPolling(): () => void {
  let timer: ReturnType<typeof setTimeout> | undefined
  let stopped = false
  const tick = async () => {
    if (stopped) return
    if (!document.hidden) await useActivity.getState().refresh()
    const { data, open } = useActivity.getState()
    timer = setTimeout(tick, open || needsPolling(data) ? 2000 : 8000)
  }
  const wake = () => {
    if (!document.hidden) {
      if (timer) clearTimeout(timer)
      tick()
    }
  }
  document.addEventListener('visibilitychange', wake)
  tick()
  return () => {
    stopped = true
    if (timer) clearTimeout(timer)
    document.removeEventListener('visibilitychange', wake)
  }
}
