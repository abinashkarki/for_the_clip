import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../api/client', async () => {
  const { mockApi } = await import('../api/mock')
  const { ApiError } = await import('../api/errors')
  const spy = { ...mockApi, saveTimeline: vi.fn(mockApi.saveTimeline) }
  return { api: spy, ApiError, newRequestId: () => `req-${Math.random().toString(36).slice(2)}`, isMock: () => true }
})

import { api } from '../api/client'
import { setHook } from '../editor/ops'
import { useEditor } from './editorStore'

const CLIP = 'short-7d1c'
const hookText = () => {
  const h = useEditor.getState().timeline!.overlays.find((o) => o.kind === 'hook')
  return h && h.kind === 'hook' ? h.text : undefined
}
const edit = (text: string, coalesce?: string) => useEditor.getState().edit((t) => setHook(t, { text }), coalesce ? { coalesce } : undefined)

beforeEach(async () => {
  await useEditor.getState().load(CLIP)
  vi.mocked(api.saveTimeline).mockClear()
})
afterEach(() => {
  vi.useRealTimers()
  useEditor.getState().reset()
})

describe('editor store', () => {
  it('loads a clip with its revision', () => {
    const s = useEditor.getState()
    expect(s.timeline).not.toBeNull()
    expect(s.revisionId).toBeTruthy()
    expect(s.save).toBe('idle')
  })

  it('autosaves once, 800 ms after the last edit, with base_revision and request_id', async () => {
    vi.useFakeTimers()
    const base = useEditor.getState().revisionId
    edit('One')
    vi.advanceTimersByTime(799)
    expect(api.saveTimeline).not.toHaveBeenCalled()
    edit('Two')
    vi.advanceTimersByTime(799)
    expect(api.saveTimeline).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(2)
    await vi.runAllTimersAsync()
    expect(api.saveTimeline).toHaveBeenCalledTimes(1)
    const [id, body] = vi.mocked(api.saveTimeline).mock.calls[0]
    expect(id).toBe(CLIP)
    expect(body.base_revision).toBe(base)
    expect(body.request_id).toMatch(/^req-/)
    expect(useEditor.getState().save).toBe('saved')
    expect(useEditor.getState().revisionId).not.toBe(base)
    expect(hookText()).toBe('Two')
  })

  it('chains the next save on the new revision', async () => {
    edit('A')
    await useEditor.getState().flushSave()
    const rev = useEditor.getState().revisionId
    edit('B')
    await useEditor.getState().flushSave()
    expect(vi.mocked(api.saveTimeline).mock.calls[1][1].base_revision).toBe(rev)
  })

  it('reuses the request id when a failed save is retried', async () => {
    vi.mocked(api.saveTimeline).mockRejectedValueOnce(new Error('offline'))
    edit('Retry me')
    await useEditor.getState().flushSave()
    expect(useEditor.getState().save).toBe('error')
    await useEditor.getState().flushSave()
    const calls = vi.mocked(api.saveTimeline).mock.calls
    expect(calls).toHaveLength(2)
    expect(calls[1][1].request_id).toBe(calls[0][1].request_id)
    expect(useEditor.getState().save).toBe('saved')
  })

  it('goes into conflict on 409 and resolves with reload or keep', async () => {
    ;(globalThis as { __mockConflictNext?: boolean }).__mockConflictNext = true
    edit('Mine')
    await useEditor.getState().flushSave()
    expect(useEditor.getState().save).toBe('conflict')
    // edits are blocked while in conflict
    edit('Ignored')
    expect(hookText()).toBe('Mine')

    await useEditor.getState().resolveConflict('reload')
    expect(useEditor.getState().save).toBe('idle')
    expect(hookText()).toContain('(edited elsewhere)')

    ;(globalThis as { __mockConflictNext?: boolean }).__mockConflictNext = true
    edit('Mine again')
    await useEditor.getState().flushSave()
    expect(useEditor.getState().save).toBe('conflict')
    await useEditor.getState().resolveConflict('keep')
    expect(useEditor.getState().save).toBe('saved')
    expect(hookText()).toBe('Mine again')
  })

  it('undo and redo restore timelines and mark the clip dirty', () => {
    edit('First')
    edit('Second')
    useEditor.getState().undo()
    expect(hookText()).toBe('First')
    useEditor.getState().undo()
    expect(hookText()).not.toBe('First')
    expect(useEditor.getState().save).toBe('dirty')
    useEditor.getState().redo()
    expect(hookText()).toBe('First')
    edit('Branch')
    expect(useEditor.getState().future).toHaveLength(0)
  })

  it('coalesces rapid edits with the same key into one undo step', () => {
    edit('a', 'hook-text')
    edit('ab', 'hook-text')
    edit('abc', 'hook-text')
    expect(useEditor.getState().past).toHaveLength(1)
    useEditor.getState().undo()
    expect(hookText()).not.toBe('a')
  })

  it('ignores no-op edits', () => {
    useEditor.getState().edit((t) => t)
    expect(useEditor.getState().save).toBe('idle')
    expect(useEditor.getState().past).toHaveLength(0)
  })
})
