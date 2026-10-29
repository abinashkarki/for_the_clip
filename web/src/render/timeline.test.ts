import { describe, expect, it } from 'vitest'
import { baseTimeline, wordsBetween } from './fixtures'
import {
  duration,
  effectiveShots,
  formatTime,
  normalizeTimeline,
  programMap,
  programToSource,
  programWords,
  sourceToProgram,
} from './timeline'

describe('program_map', () => {
  it('splits segments at cuts and assigns program offsets', () => {
    const map = programMap(baseTimeline())
    expect(map).toEqual([
      { source_id: 'a', src_start: 10, src_end: 14, at: 0, segment_id: 's1' },
      { source_id: 'a', src_start: 15, src_end: 20, at: 4, segment_id: 's1' },
      { source_id: 'a', src_start: 30, src_end: 35, at: 9, segment_id: 's2' },
    ])
  })
  it('duration is the sum of kept ranges', () => {
    expect(duration(baseTimeline())).toBe(14)
  })
  it('a segment with no cuts is a single range', () => {
    const tl = baseTimeline({ segments: [{ id: 's', source_id: 'a', start: 1, end: 3, cuts: [] }] })
    expect(programMap(tl)).toHaveLength(1)
    expect(duration(tl)).toBe(2)
  })
})

describe('time mapping', () => {
  const map = programMap(baseTimeline())
  it('program -> source', () => {
    expect(programToSource(map, 0)).toMatchObject({ index: 0, time: 10 })
    expect(programToSource(map, 4)).toMatchObject({ index: 1, time: 15 })
    expect(programToSource(map, 10)).toMatchObject({ index: 2, time: 31 })
    expect(programToSource(map, 99)).toMatchObject({ index: 2, time: 35 })
  })
  it('source -> program, null inside a cut', () => {
    expect(sourceToProgram(map, 'a', 16)).toBe(5)
    expect(sourceToProgram(map, 'a', 14.5)).toBeNull()
    expect(sourceToProgram(map, 'a', 25)).toBeNull()
    expect(sourceToProgram(map, 'b', 16)).toBeNull()
  })
})

describe('program_words', () => {
  it('includes words by midpoint and maps clipped times', () => {
    const words = [
      ...wordsBetween(10, 12), // 4 words in first range
      { key: 'a:90', source_id: 'a', word_id: 90, text: 'edge', start: 13.8, end: 14.4 }, // mid 14.1 -> cut
      { key: 'a:91', source_id: 'a', word_id: 91, text: 'in', start: 13.6, end: 14.2 }, // mid 13.9 -> kept, clipped to 14
      { key: 'a:92', source_id: 'a', word_id: 92, text: 'late', start: 30.2, end: 30.7 },
    ]
    const pw = programWords(baseTimeline(), words)
    expect(pw.map((w) => w.key)).toEqual(['a:0', 'a:1', 'a:2', 'a:3', 'a:91', 'a:92'])
    const clipped = pw.find((w) => w.key === 'a:91')!
    expect(clipped.start).toBeCloseTo(3.6)
    expect(clipped.end).toBeCloseTo(4.0)
    expect(pw.find((w) => w.key === 'a:92')!.start).toBeCloseTo(9.2)
    expect(pw.find((w) => w.key === 'a:92')!.range).toBe(2)
  })
  it('carries emphasis and hidden flags', () => {
    const tl = baseTimeline()
    tl.captions.emphasis = ['a:1']
    tl.captions.hidden = ['a:2']
    const pw = programWords(tl, wordsBetween(10, 12))
    expect(pw[1].emphasis).toBe(true)
    expect(pw[2].hidden).toBe(true)
    expect(pw[0].emphasis).toBe(false)
  })
})

describe('shots and normalisation', () => {
  it('canonical default is one fit shot over the programme', () => {
    const shots = effectiveShots(baseTimeline())
    expect(shots).toHaveLength(1)
    expect(shots[0]).toMatchObject({ layout: 'fit', start: 0, end: 14 })
  })
  it('normalizeTimeline snaps the last shot and drops shots past the end', () => {
    const tl = baseTimeline({
      shots: [
        { id: 'a', start: 0, end: 8, layout: 'fill', regions: [{ keyframes: [{ t: 0, x: 0.5, y: 0.5, zoom: 1 }] }] },
        { id: 'b', start: 8, end: 20, layout: 'fit', regions: [{ keyframes: [{ t: 8, x: 0.5, y: 0.5, zoom: 1 }] }] },
      ],
    })
    tl.segments[1].end = 30.5 // programme now 9.5
    const n = normalizeTimeline(tl)
    expect(n.shots.at(-1)!.end).toBeCloseTo(9.5)
    tl.segments[1].end = 30.01 // programme 9.01 -> shot b starts at 8, still kept
    expect(normalizeTimeline(tl).shots).toHaveLength(2)
    tl.segments[0].cuts = [{ start: 11, end: 18, reason: 'manual' }] // programme 3 + 5 = 8.01 -> b dropped
    const n2 = normalizeTimeline(tl)
    expect(n2.shots).toHaveLength(1)
    expect(n2.shots[0].end).toBeCloseTo(duration(tl))
  })
  it('clamps hook overlays to the programme', () => {
    const tl = baseTimeline({ overlays: [{ id: 'h', kind: 'hook', text: 'x', start: 10, end: 40, style: 'hook_box' }] })
    const n = normalizeTimeline(tl)
    expect((n.overlays[0] as { end: number }).end).toBeCloseTo(14)
  })
})

describe('formatTime', () => {
  it('formats', () => {
    expect(formatTime(65.4)).toBe('1:05')
    expect(formatTime(5.25, true)).toBe('0:05.25')
  })
})
