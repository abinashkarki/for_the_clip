import { describe, expect, it } from 'vitest'
import { mockApi } from '../api/mock'
import type { ProjectSummary } from '../api/types'
import { chipCounts, chipOf, daysUntil, deletesInLabel, formatBytes, isEmptyFailure, sourceFreesLabel, trashedProjects, visibleProjects } from './libraryLogic'

const base: ProjectSummary = {
  id: 'a', name: 'A', source_ids: [], status: 'ready', clip_count: 3, created_at: '2026-10-01T00:00:00Z', thumbnail_url: null,
  lifecycle: 'active', last_activity_at: '2026-10-10T00:00:00Z', keep_source: false, source_expires_at: null,
  size_bytes: { source: 2e9, working: 1e8, exports: 5e7 }, trashed_at: null, purge_at: null,
}
const mk = (o: Partial<ProjectSummary>): ProjectSummary => ({ ...base, ...o })
const NOW = new Date('2026-10-28T12:00:00Z').getTime()

describe('library logic', () => {
  it('classifies projects into chips', () => {
    expect(chipOf(mk({}))).toBe('ready')
    expect(chipOf(mk({ status: 'processing' }))).toBe('active')
    expect(chipOf(mk({ status: 'needs_attention' }))).toBe('active')
    expect(chipOf(mk({ status: 'failed' }))).toBe('active')
    expect(chipOf(mk({ lifecycle: 'source_freed' }))).toBe('freed')
  })

  it('filters, hides trash, counts and sorts by last activity', () => {
    const list = [
      mk({ id: 'old', last_activity_at: '2026-10-01T00:00:00Z' }),
      mk({ id: 'new', last_activity_at: '2026-10-20T00:00:00Z' }),
      mk({ id: 'freed', lifecycle: 'source_freed' }),
      mk({ id: 'run', status: 'processing' }),
      mk({ id: 'bin', lifecycle: 'trashed', trashed_at: '2026-10-27T00:00:00Z' }),
    ]
    expect(visibleProjects(list, 'all')[0].id).toBe('new')
    expect(visibleProjects(list, 'all').map((p) => p.id)).not.toContain('bin')
    expect(visibleProjects(list, 'ready').map((p) => p.id)).toEqual(['new', 'old'])
    expect(visibleProjects(list, 'freed').map((p) => p.id)).toEqual(['freed'])
    expect(visibleProjects(list, 'active').map((p) => p.id)).toEqual(['run'])
    expect(trashedProjects(list).map((p) => p.id)).toEqual(['bin'])
    expect(chipCounts(list)).toEqual({ all: 4, active: 1, ready: 2, freed: 1, trash: 1 })
  })

  it('counts days up and never goes negative', () => {
    expect(daysUntil(null, NOW)).toBeNull()
    expect(daysUntil('2026-10-31T12:00:00Z', NOW)).toBe(3)
    expect(daysUntil('2026-10-28T13:00:00Z', NOW)).toBe(1)
    expect(daysUntil('2026-10-20T00:00:00Z', NOW)).toBe(0)
  })

  it('words the countdown labels', () => {
    expect(sourceFreesLabel(mk({ source_expires_at: '2026-10-29T12:00:00Z' }), NOW)).toBe('Source frees in 1 day')
    expect(sourceFreesLabel(mk({ source_expires_at: '2026-11-05T12:00:00Z' }), NOW)).toBe('Source frees in 8 days')
    expect(sourceFreesLabel(mk({}), NOW)).toBeNull()
    expect(deletesInLabel(mk({ lifecycle: 'trashed', purge_at: '2026-11-02T12:00:00Z' }), NOW)).toBe('Deletes in 5 days')
  })

  it('formats sizes and spots empty failures', () => {
    expect(formatBytes(0)).toBe('0 MB')
    expect(formatBytes(38e6)).toBe('38 MB')
    expect(formatBytes(1.84e9)).toBe('1.8 GB')
    expect(isEmptyFailure(mk({ status: 'failed', clip_count: 0 }))).toBe(true)
    expect(isEmptyFailure(mk({ status: 'failed', clip_count: 2 }))).toBe(false)
  })
})

describe('mock lifecycle transitions', () => {
  it('seeds every lifecycle and hides trash by default', async () => {
    const visible = await mockApi.listProjects()
    const all = await mockApi.listProjects({ includeTrashed: true })
    expect(visible.some((p) => p.lifecycle === 'trashed')).toBe(false)
    expect(all.some((p) => p.lifecycle === 'trashed')).toBe(true)
    expect(visible.some((p) => p.lifecycle === 'source_freed')).toBe(true)
    expect(visible.some((p) => p.keep_source)).toBe(true)
  })

  it('refuses destructive operations and does not advertise deletion dates', async () => {
    const before = await mockApi.listProjects({ includeTrashed: true })
    expect(before.every((p) => p.source_expires_at === null && p.purge_at === null)).toBe(true)
    await expect(mockApi.trashProject('col-55')).rejects.toMatchObject({ status: 503 })
    await expect(mockApi.deleteProject('col-22')).rejects.toMatchObject({ status: 503 })
    await expect(mockApi.freeSource('col-33')).rejects.toMatchObject({ status: 503 })
    await expect(mockApi.dismissSource('job-2004')).rejects.toMatchObject({ status: 503 })
    await expect(mockApi.freeStorageNow()).rejects.toMatchObject({ status: 503 })
    expect((await mockApi.listProjects({ includeTrashed: true })).map((p) => [p.id, p.lifecycle])).toEqual(before.map((p) => [p.id, p.lifecycle]))
    expect((await mockApi.getStorage()).reclaimable_bytes).toBe(0)
  })

  it('can restore a previously trashed project', async () => {
    expect((await mockApi.untrashProject('col-22')).lifecycle).toBe('active')
  })
})
