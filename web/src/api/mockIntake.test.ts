import { describe, expect, it } from 'vitest'
import { mockApi } from './mock'

describe('mock intake', () => {
  it('inspects a link with sizes per option and rejects an unreadable one', async () => {
    const info = await mockApi.inspectSource('https://video.example/watch?v=1')
    expect(info.options.map((o) => o.id)).toEqual(['standard', 'high', 'small'])
    expect(info.estimates.download_bytes.standard).toBeGreaterThan(info.estimates.download_bytes.small!)
    await expect(mockApi.inspectSource('https://bad.example/x')).rejects.toMatchObject({ status: 422 })
  })

  it('records quality and range, shows staged progress, cancels and retries', async () => {
    const { source_id } = await mockApi.addSource({ url: 'https://video.example/watch?v=2', quality: 'small', range: { start: 60, end: 660 } })
    const added = (await mockApi.listSources()).find((s) => s.id === source_id)!
    expect(added).toMatchObject({ quality: 'small', range: { start: 60, end: 660 }, kind: 'url', cancellable: true, duration: 600 })
    expect(added.stages!.map((s) => s.id)).toEqual(['download', 'merge', 'preview', 'transcribe', 'index'])
    expect(added.stages![0].status).toBe('active')

    const activity = await mockApi.getActivity()
    expect(activity.items.find((i) => i.id === source_id)).toMatchObject({ kind: 'source', actions: [{ id: 'cancel', label: 'Cancel' }] })
    expect(activity.running).toBeGreaterThan(0)

    const cancelled = await mockApi.cancelSource(source_id)
    expect(cancelled).toMatchObject({ status: 'cancelled', retryable: true, cancellable: false })
    const after = await mockApi.getActivity()
    expect(after.items.find((i) => i.id === source_id)).toMatchObject({ state: 'attention', actions: [{ id: 'retry', label: 'Retry' }] })

    const retried = await mockApi.retrySource(source_id)
    expect(retried.status).toBe('ingesting')
  })

  it('lists a running export in the activity feed', async () => {
    const clip = (await mockApi.getProject('col-55')).clips[0]
    const detail = await mockApi.getClip(clip.id)
    const { job_id } = await mockApi.exportClip(clip.id, { revision_id: detail.revision_id, quality: 'preview' })
    const item = (await mockApi.getActivity()).items.find((i) => i.id === job_id)
    expect(item).toMatchObject({ kind: 'export', state: 'running' })
    expect(item!.title).toContain('Exporting')
  })
})
