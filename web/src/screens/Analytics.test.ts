import { describe, expect, it } from 'vitest'
import { matchesPublishingFilter, summarizePublishing } from './Analytics'
import type { Post } from '../api/publish'

const post = (status: Post['status'], views?: number, rendering = false): Post => ({
  status, rendering, metrics: views === undefined ? null : { views },
} as Post)

describe('publishing overview', () => {
  it('distinguishes absent and partial metrics from an actual zero', () => {
    expect(summarizePublishing([post('draft')]).views).toBeNull()
    expect(summarizePublishing([post('published', 0)]).views).toBe(0)
    expect(summarizePublishing([post('published', 120), post('published')])).toMatchObject({ views: 120, partialViews: true, live: 2 })
  })
  it('filters clips by their actual post states, including mixed states', () => {
    const items = [post('failed'), post('published', 2), post('scheduled'), post('draft', undefined, true)]
    expect(['all', 'attention', 'live', 'scheduled', 'drafts'].map((filter) => matchesPublishingFilter(items, filter as 'all' | 'attention' | 'live' | 'scheduled' | 'drafts'))).toEqual([true, true, true, true, true])
    expect(matchesPublishingFilter([post('draft')], 'attention')).toBe(false)
    expect(summarizePublishing(items)).toMatchObject({ attention: 1, live: 1, scheduled: 1, drafts: 1, uploading: 1 })
  })
})
