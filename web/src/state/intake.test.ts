import { describe, expect, it } from 'vitest'
import type { ActivityItem, InspectResult, IntakeStage, SourceSummary } from '../api/types'
import { badge, clampRange, estimateLines, inspectedResolution, formatBytes, formatClock, formatDuration, formatEta, headline, parseClock, rangeLabel, sortActivity, validateRange } from './intake'

const stage = (id: IntakeStage['id'], status: IntakeStage['status'], extra: Partial<IntakeStage> = {}): IntakeStage => ({
  id, label: id, status, fraction: null, detail: null, started_at: null, updated_at: null, eta_seconds: null, stalled: false, ...extra,
})
const source = (extra: Partial<SourceSummary> = {}): SourceSummary => ({
  id: 's', title: 'Talk', status: 'ingesting', progress: 0.3, duration: 9300, thumbnail_url: null, media_url: null, error: null, ...extra,
})

describe('formatting', () => {
  it('writes waits the way the server does', () => {
    expect(formatEta(20)).toBe('~20 s left')
    expect(formatEta(125)).toBe('~2 min left')
    expect(formatEta(4320)).toBe('~1 h 12 min left')
    expect(formatEta(7200)).toBe('~2 h left')
    expect(formatEta(null)).toBe('')
    expect(formatEta(Number.NaN)).toBe('')
  })
  it('formats sizes, lengths and clocks', () => {
    expect(formatBytes(3.13e9)).toBe('3.13 GB')
    expect(formatBytes(151e6)).toBe('151 MB')
    expect(formatBytes(42e6)).toBe('42.0 MB')
    expect(formatBytes(null)).toBe('')
    expect(formatDuration(9300)).toBe('2 h 35 min')
    expect(formatDuration(45)).toBe('45 s')
    expect(formatClock(9300)).toBe('2:35:00')
    expect(formatClock(75)).toBe('1:15')
  })
})

describe('range input', () => {
  it('reads clock times in several forms', () => {
    expect(parseClock('1:02:03')).toBe(3723)
    expect(parseClock('12:30')).toBe(750)
    expect(parseClock('90')).toBe(90)
    expect(parseClock('1h2m3s')).toBe(3723)
    expect(parseClock('5m')).toBe(300)
    expect(parseClock('')).toBeNull()
    expect(parseClock('soon')).toBeNull()
    expect(parseClock('1:2:3:4')).toBeNull()
  })
  it('explains what is wrong with a range', () => {
    expect(validateRange(null, 10, 100)).toMatch(/start and an end/)
    expect(validateRange(50, 10, 100)).toMatch(/end must come after/)
    expect(validateRange(200, 300, 100)).toMatch(/only 1:40 long/)
    expect(validateRange(10, 50, 100)).toBeNull()
    expect(clampRange({ start: 10, end: 500 }, 100)).toEqual({ start: 10, end: 100 })
    expect(rangeLabel({ start: 60, end: 3725 })).toBe('1:00 to 1:02:05')
    expect(rangeLabel(null)).toBe('')
  })
})

describe('headline', () => {
  it('leads with the first running stage and lists the others beside it', () => {
    const h = headline(
      source({ stages: [stage('download', 'active', { fraction: 0.6, detail: '1.83 of 3.13 GB · 3.4 MB/s · ~6 min left', eta_seconds: 380 }), stage('transcribe', 'active', { detail: '32:40 of 2:35:00' })] }),
    )
    expect(h.text).toBe('download')
    expect(h.detail).toBe('1.83 of 3.13 GB · 3.4 MB/s · ~6 min left')
    expect(h.eta).toBe('~6 min left')
    expect(h.also.map((s) => s.id)).toEqual(['transcribe'])
    expect(h.stalled).toBe(false)
  })
  it('carries the stall reason of whichever stage is stuck', () => {
    const h = headline(source({ stages: [stage('download', 'active', { stalled: true, stall_reason: 'No data has arrived.' })] }))
    expect(h.stalled).toBe(true)
    expect(h.reason).toBe('No data has arrived.')
  })
  it('says why nothing is running: queue position, a plain wait, a failure, a cancel', () => {
    const queue = { lane: 'local' as const, ahead: 1, position: 2, message: 'Waiting · 1 video ahead (transcribing, ~8 min left)' }
    expect(headline(source({ queue, stages: [stage('download', 'pending')] })).text).toBe('Waiting · 1 video ahead (transcribing, ~8 min left)')
    expect(headline(source({ stages: [] })).text).toBe('Waiting to start')
    expect(headline(source({ status: 'failed', error: 'HTTP 403' }))).toMatchObject({ text: 'Stopped', detail: 'HTTP 403' })
    expect(headline(source({ status: 'cancelled' })).text).toBe('Cancelled')
  })
})

describe('pre-flight estimates', () => {
  const info: InspectResult = {
    title: 'Talk', duration: 9300, thumbnail_url: null, uploader: null, options: [],
    estimates: { download_bytes: { standard: 3.13e9, small: 1.12e9, audio_first: 1.51e8 }, download_seconds: { standard: 920, small: 330, audio_first: 44 }, transcribe_seconds: 372 },
  }
  it('shows the inspected resolution and when it falls below the quality limit', () => {
    const selected = { ...info, options: [{ id: 'high' as const, label: '720p', height: 720, download_bytes: null }] }
    expect(inspectedResolution(selected, 'high')).toContain('720p for this choice (below the 2160p limit)')
    expect(inspectedResolution(selected, 'standard')).toContain('could not be determined')
    expect(inspectedResolution({ ...selected, options: [{ id: 'standard', label: '1080p', height: 1080, download_bytes: null }] }, 'standard')).not.toContain('below')
  })
  it('gives size, speed and transcription time for the chosen quality', () => {
    expect(estimateLines(info, 'standard', null)).toEqual([
      'Estimated download: about 3.13 GB, around 15 min. Actual size and time can vary.',
      'Estimated free space needed: about 9.39 GB for temporary download and processing files. This is an estimate based on the full video.',
      'Transcription should take about 6 min on this computer.',
    ])
    expect(estimateLines(info, 'small', null)[0]).toContain('1.12 GB')
  })
  it('scales every number to the part of the video chosen', () => {
    const lines = estimateLines(info, 'standard', { start: 0, end: 930 })
    expect(lines[0]).toContain('313 MB')
    expect(lines[1]).toContain('9.39 GB')
    expect(lines[2]).toContain('37 s')
  })
  it('omits what is unknown', () => {
    expect(estimateLines({ ...info, estimates: { download_bytes: {}, download_seconds: {}, transcribe_seconds: null } }, 'standard', null)).toEqual(['Download size is unavailable for this link; actual size and time can vary.', 'Estimated free space needed: unknown because the download size is unavailable.'])
  })
})

describe('activity', () => {
  const item = (state: ActivityItem['state']): ActivityItem => ({
    kind: 'source', id: state, title: state, state, status: state, fraction: 0, message: null, eta_seconds: null, stalled: false, stall_reason: null,
    thumbnail_url: null, stages: [], queue: null, actions: [], open: { route: 'library' },
  })
  it('lists running work first, then waiting, then what needs attention', () => {
    expect(sortActivity([item('done'), item('attention'), item('waiting'), item('running')]).map((i) => i.state)).toEqual(['running', 'waiting', 'attention', 'done'])
  })
  it('counts running work and flags attention without counting it', () => {
    expect(badge(null)).toEqual({ count: 0, attention: false })
    expect(badge({ items: [], running: 2, attention: 1 })).toEqual({ count: 2, attention: true })
  })
})
