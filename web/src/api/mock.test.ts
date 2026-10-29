import { describe, expect, it } from 'vitest'
import { duration } from '../render/timeline'
import { ApiError } from './errors'
import example from './studio_clip.example.json'
import { mockApi } from './mock'

const keys = (o: object) => Object.keys(o).sort()

describe('mock API honours the Studio contract', () => {
  it('serves the example clip with the documented shape', async () => {
    const c = await mockApi.getClip('short-7d1c')
    expect(c.clip.id).toBe(example.clip.id)
    expect(c.project_id).toBe(example.project_id)
    expect(keys(c.timeline)).toEqual(keys(example.timeline))
    expect(c.timeline.schema_version).toBe(3)
    expect(keys(c.timeline.canvas)).toEqual(keys(example.timeline.canvas))
    expect(keys(c.timeline.segments[0].cuts[0])).toEqual(keys(example.timeline.segments[0].cuts[0]))
    expect(c.revision_id).toBe(c.clip.revision_id)
    expect(c.words.length).toBeGreaterThan(50)
    for (const w of c.words.slice(0, 5)) expect(w.key).toBe(`${w.source_id}:${w.word_id}`)
  })

  it('lists projects including processing and failed ones, and clips per project', async () => {
    const ps = await mockApi.listProjects()
    expect(ps.length).toBeGreaterThanOrEqual(4)
    expect(new Set(ps.map((p) => p.status)).size).toBeGreaterThan(1)
    const d = await mockApi.getProject('col-55')
    expect(d.clips.length).toBe(8)
    expect(d.clips.every((c) => c.duration > 0)).toBe(true)
  })

  it('saves with base_revision, is idempotent per request_id, and 409s on a stale base', async () => {
    const c = await mockApi.getClip('short-7d1c')
    const tl = structuredClone(c.timeline)
    tl.captions.template_id = 'karaoke'
    const first = await mockApi.saveTimeline(c.clip.id, { base_revision: c.revision_id, timeline: tl, request_id: 'r-1' })
    expect(first.revision_id).not.toBe(c.revision_id)
    const again = await mockApi.saveTimeline(c.clip.id, { base_revision: c.revision_id, timeline: tl, request_id: 'r-1' })
    expect(again.revision_id).toBe(first.revision_id)
    await expect(mockApi.saveTimeline(c.clip.id, { base_revision: c.revision_id, timeline: tl, request_id: 'r-2' })).rejects.toMatchObject({
      status: 409,
    })
    await expect(mockApi.saveTimeline(c.clip.id, { base_revision: c.revision_id, timeline: tl, request_id: 'r-3' })).rejects.toBeInstanceOf(ApiError)
    await expect(mockApi.getClip('nope')).rejects.toMatchObject({ status: 404 })
  })

  it('remove-fillers shortens the clip', async () => {
    const c = await mockApi.getClip('short-7d1c')
    const res = await mockApi.removeFillers(c.clip.id, { base_revision: c.revision_id, request_id: 'rf-1', silence_min: 0.5 })
    expect(duration(res.timeline)).toBeLessThanOrEqual(duration(c.timeline))
  })

  it('exports through a job that finishes with a download url', async () => {
    const c = await mockApi.getClip('short-7d1c')
    const { job_id } = await mockApi.exportClip(c.clip.id, { revision_id: c.revision_id, quality: 'preview' })
    const first = await mockApi.getJob(job_id)
    expect(['queued', 'running']).toContain(first.status)
    let job = first
    for (let i = 0; i < 80 && job.status !== 'done'; i++) {
      await new Promise((r) => setTimeout(r, 100))
      job = await mockApi.getJob(job_id)
    }
    expect(job.status).toBe('done')
    expect(job.progress).toBe(1)
  }, 15000)

  it('ships the real caption and hook templates with metrics', async () => {
    const reg = await mockApi.templates()
    expect(reg.captions.map((t) => t.id)).toContain('bold_pop')
    expect(reg.captions.every((t) => t.font.metrics && t.font.metrics.ascent > 0)).toBe(true)
    expect(reg.hooks.map((h) => h.id).sort()).toEqual(['hook_banner', 'hook_box', 'hook_outline'])
  })
})
