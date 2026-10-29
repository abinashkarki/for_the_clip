import { describe, expect, it } from 'vitest'
import {
  checkSchedule,
  curvePaths,
  defaultSchedule,
  fmtCount,
  fmtSeconds,
  groupDrops,
  mapRetention,
  parseHashtags,
  publishDefaults,
  shortsChecks,
  statusLabel,
  suggestHashtags,
  toApiTime,
  watchRatioAt,
  zonedToUtc,
  type RetentionPoint,
  groupPostsByProject,
  visibleClips,
} from './publishLogic'

describe('time zones', () => {
  it('converts Sydney summer time to UTC', () => {
    expect(toApiTime(zonedToUtc('2026-11-02T18:30', 'Australia/Sydney')!)).toBe('2026-11-02T07:30:00Z')
  })
  it('handles daylight saving ending in New York', () => {
    expect(toApiTime(zonedToUtc('2026-10-31T12:00', 'America/New_York')!)).toBe('2026-10-31T16:00:00Z')
    expect(toApiTime(zonedToUtc('2026-11-02T12:00', 'America/New_York')!)).toBe('2026-11-02T17:00:00Z')
  })
  it('treats UTC as identity and rejects malformed input', () => {
    expect(toApiTime(zonedToUtc('2026-01-05T09:15', 'UTC')!)).toBe('2026-01-05T09:15:00Z')
    expect(zonedToUtc('tomorrow', 'UTC')).toBeNull()
    expect(zonedToUtc('', 'UTC')).toBeNull()
  })
  it('defaults the schedule to 18:00 the next day in the chosen zone', () => {
    expect(defaultSchedule(new Date('2026-11-02T01:00:00Z'), 'Australia/Sydney')).toBe('2026-11-03T18:00')
  })
  it('requires a real time between 5 minutes and 6 months ahead', () => {
    const now = new Date('2026-11-02T00:00:00Z')
    expect(checkSchedule('', 'UTC', now).ok).toBe(false)
    expect(checkSchedule('2026-11-02T00:03', 'UTC', now).ok).toBe(false)
    expect(checkSchedule('2027-12-01T00:00', 'UTC', now).ok).toBe(false)
    const ok = checkSchedule('2026-11-02T10:00', 'UTC', now)
    expect(ok.ok && toApiTime(ok.at)).toBe('2026-11-02T10:00:00Z')
  })
})

describe('shorts checks', () => {
  it('passes short vertical clips', () => {
    expect(shortsChecks({ duration: 45, width: 1080, height: 1920 }).every((c) => c.ok)).toBe(true)
  })
  it('flags long and horizontal clips', () => {
    const c = shortsChecks({ duration: 200, width: 1920, height: 1080 })
    expect(c.map((x) => x.ok)).toEqual([false, false])
    expect(c[0].text).toContain('3 minute')
  })
  it('skips checks it has no data for', () => {
    expect(shortsChecks({ duration: null })).toEqual([])
  })
})

describe('title, description and hashtags', () => {
  it('prefills from the clip title and hook', () => {
    const d = publishDefaults("Khanmigo asks how you'd approach it", 'This AI tutor refuses to give answers')
    expect(d.title).toBe("Khanmigo asks how you'd approach it")
    expect(d.description).toBe('This AI tutor refuses to give answers')
    expect(d.hashtags.length).toBeGreaterThan(0)
    expect(d.hashtags.every((t) => /^[a-z0-9]+$/.test(t))).toBe(true)
  })
  it('skips a hook that repeats the title and trims long titles', () => {
    expect(publishDefaults('Same line', 'same line').description).toBe('')
    expect(publishDefaults('x'.repeat(150)).title).toHaveLength(100)
    expect(publishDefaults('   ').title).toBe('Untitled clip')
  })
  it('prefers the post copy written at generation', () => {
    const d = publishDefaults('Math as oral culture', 'Why do mathematicians need a chalkboard?', {
      title: 'The real way mathematicians learn',
      description: 'A mathematician on how ideas spread.',
      hashtags: ['Math', '#mathematics', 'math'],
    })
    expect(d).toEqual({ title: 'The real way mathematicians learn', description: 'A mathematician on how ideas spread.', hashtags: ['math', 'mathematics'] })
  })
  it('falls back field by field when generated copy is missing', () => {
    const d = publishDefaults('Label', 'Hook text', { title: '', description: '', hashtags: [] })
    expect(d.title).toBe('Label')
    expect(d.description).toBe('Hook text')
    expect(d.hashtags.length).toBeGreaterThan(0)
  })
  it('suggests content words, not stop words', () => {
    expect(suggestHashtags('Why students stop copying and start thinking')).toEqual(['students', 'stop', 'copying'])
  })
  it('parses hashtag text into clean unique tags', () => {
    expect(parseHashtags('#AI, #learning  ai #learning! ##x-y')).toEqual(['AI', 'learning', 'ai', 'xy'])
    expect(parseHashtags('')).toEqual([])
  })
})

const CURVE: RetentionPoint[] = [[0, 1, 0.5], [0.4, 0.9, 0.5], [0.6, 0.4, 0.1], [1, 0.3, 0.2]]
const WORDS = ['one', 'two', 'three', 'four', 'five'].map((text, i) => ({ text, start: i, end: i + 0.9 }))

describe('retention mapping', () => {
  it('interpolates the watch ratio', () => {
    expect(watchRatioAt(CURVE, 0.5)).toBeCloseTo(0.65)
    expect(watchRatioAt(CURVE, 2)).toBe(0.3)
    expect(watchRatioAt([], 0.5)).toBe(0)
  })
  it('flags the word where the audience leaves, like the server', () => {
    const m = mapRetention(CURVE, WORDS, 5)
    expect(m.drops.map((i) => m.words[i].text)).toEqual(['three'])
    expect(m.words[0].watch_ratio).toBeGreaterThan(m.words[4].watch_ratio)
  })
  it('returns nothing without data and flags nothing on a flat curve', () => {
    expect(mapRetention([], WORDS, 5).words).toEqual([])
    expect(mapRetention([[0, 0.8, 0.5], [1, 0.8, 0.5]], WORDS, 5).drops).toEqual([])
  })
  it('merges adjacent drop words into one phrase', () => {
    const words = ['a', 'b', 'c', 'd', 'e'].map((text, i) => ({ index: i, text, start: i, end: i + 1, ratio: 0, watch_ratio: 0, relative: null, loss: 0.1 }))
    expect(groupDrops(words, [3, 1, 2]).map((g) => g.text)).toEqual(['b c d'])
    expect(groupDrops(words, [0, 4]).map((g) => g.text)).toEqual(['a', 'e'])
    expect(groupDrops(words, [9])).toEqual([])
  })
})

describe('chart geometry', () => {
  const plot = { w: 100, h: 50, left: 10, right: 10, top: 5, bottom: 5 }
  it('maps the curve into the plot rectangle', () => {
    const { line, area } = curvePaths([[0, 1, null], [1, 0.5, null]], plot)
    expect(line).toBe('M10.0 5.0L90.0 25.0')
    expect(area.endsWith('L90.0 45L10.0 45Z')).toBe(true)
  })
  it('keeps the axis at 100% and rounds up to a quarter for re-watched curves', () => {
    expect(curvePaths([[0, 1.4, null]], plot).max).toBe(1.5)
    expect(curvePaths([[0, 1.02, null]], plot).max).toBe(1.25)
    expect(curvePaths([[0, 0.9, null]], plot).max).toBe(1)
    expect(curvePaths([], plot).line).toBe('')
  })
})

describe('formatting', () => {
  it('abbreviates counts', () => {
    expect([0, 999, 1200, 15300, 2_400_000, null].map(fmtCount)).toEqual(['0', '999', '1.2K', '15K', '2.4M', 'n/a'])
  })
  it('formats durations', () => {
    expect([null, 19, 75].map(fmtSeconds)).toEqual(['n/a', '19 s', '1:15'])
  })
  it('labels post states in plain words', () => {
    expect(statusLabel({ status: 'published' }).tone).toBe('ok')
    expect(statusLabel({ status: 'scheduled', rendering: true }).text).toBe('Exporting video')
    expect(statusLabel({ status: 'scheduled', external_id: 'x', scheduled_at: '2026-01-01T00:00:00Z' }).text).toContain('goes live')
    expect(statusLabel({ status: 'failed' }).tone).toBe('bad')
  })
})

describe('groupPostsByProject', () => {
  const mk = (id: string, short: string, status: string, project: string | null, over: Record<string, unknown> = {}) => ({
    id, short_id: short, title: `Clip ${short}`, platform: 'youtube', channel_name: 'Main', status,
    created_at: '2026-10-01T00:00:00Z', published_at: null,
    project_id: project, project_name: project ? `Project ${project}` : null, project_deleted: false, ...over,
  })
  const posts = [
    mk('1', 's1', 'published', 'a', { published_at: '2026-10-02T00:00:00Z' }),
    mk('2', 's2', 'failed', 'b', { created_at: '2026-10-10T00:00:00Z' }),
    mk('3', 's3', 'scheduled', 'b'),
    mk('4', 's4', 'published', 'c', { project_deleted: true, published_at: '2026-10-20T00:00:00Z' }),
    mk('5', 's5', 'draft', null, { project_deleted: true }),
  ]

  it('sorts projects by latest activity and puts deleted projects last', () => {
    const groups = groupPostsByProject(posts)
    expect(groups.map((g) => g.key)).toEqual(['b', 'a', '__deleted__'])
    expect(groups[0]).toMatchObject({ name: 'Project b', failed: 1, scheduled: 1, published: 0, postCount: 2 })
    expect(groups[2].clips.map((c) => c.shortId).sort()).toEqual(['s4', 's5'])
    expect(groups[2]).toMatchObject({ deleted: true, projectId: null })
  })
  it('hides projects with no matching clip under a filter', () => {
    expect(groupPostsByProject(posts, 'attention').map((g) => g.key)).toEqual(['b'])
    expect(groupPostsByProject(posts, 'drafts').map((g) => g.key)).toEqual(['__deleted__'])
    expect(groupPostsByProject(posts, 'live').map((g) => g.key)).toEqual(['a', '__deleted__'])
  })
  it('searches clip text, accounts and project names', () => {
    expect(groupPostsByProject(posts, 'all', 'project a').map((g) => g.key)).toEqual(['a'])
    expect(groupPostsByProject(posts, 'all', 'clip s3')[0].clips.map((c) => c.shortId)).toEqual(['s3'])
    expect(groupPostsByProject(posts, 'all', 'zzz')).toEqual([])
  })
  it('limits long clip lists to five until expanded', () => {
    const items = [1, 2, 3, 4, 5, 6, 7]
    expect(visibleClips(items, false)).toHaveLength(5)
    expect(visibleClips(items, true)).toHaveLength(7)
    expect(visibleClips(items.slice(0, 5), false)).toHaveLength(5)
  })
})
