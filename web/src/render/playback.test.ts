import { describe, expect, it } from 'vitest'
import { baseTimeline } from './fixtures'
import { type MediaLike, PlaybackEngine } from './playback'
import { programMap } from './timeline'

class FakeMedia implements MediaLike {
  currentTime = 0
  paused = true
  seeking = false
  readyState = 4
  videoWidth = 1280
  videoHeight = 720
  muted = false
  seeks: number[] = []
  plays = 0
  constructor(public id: string) {}
  play() {
    this.paused = false
    this.plays++
  }
  pause() {
    this.paused = true
  }
  advance(dt: number) {
    if (!this.paused) this.currentTime += dt
  }
}

function setup() {
  const made: FakeMedia[] = []
  const engine = new PlaybackEngine({
    createMedia: (id) => {
      const m = new FakeMedia(id)
      // record seeks
      let t = 0
      Object.defineProperty(m, 'currentTime', {
        get: () => t,
        set: (v: number) => {
          t = v
          m.seeks.push(v)
        },
      })
      made.push(m)
      return m
    },
  })
  engine.setRanges(programMap(baseTimeline()))
  return { engine, made }
}

/** Advance whichever elements are playing by dt, then run one scheduler step. */
function step(engine: PlaybackEngine, made: FakeMedia[], dt: number) {
  made.forEach((m) => m.advance(dt))
  engine.tick()
}

describe('PlaybackEngine', () => {
  it('reports duration and parks at the first range', () => {
    const { engine } = setup()
    expect(engine.duration).toBe(14)
    expect(engine.time).toBe(0)
    expect(engine.frameSource?.currentTime).toBe(10)
  })

  it('seek maps programme time through cuts', () => {
    const { engine } = setup()
    engine.seek(5)
    expect(engine.frameSource!.currentTime).toBeCloseTo(16)
    expect(engine.time).toBe(5)
    engine.seek(100)
    expect(engine.time).toBe(14)
  })

  it('pre-seeks the standby element to the next range before the boundary', () => {
    const { engine, made } = setup()
    // standby for range 1 (src 15) is parked while range 0 plays
    const active = engine.frameSource as FakeMedia
    const standby = made.find((m) => m !== active)!
    expect(standby.currentTime).toBe(15)
    expect(standby.paused).toBe(true)
  })

  it('swaps elements across a cut without seeking the active one', () => {
    const { engine, made } = setup()
    engine.play()
    const first = engine.frameSource as FakeMedia
    let swaps = 0
    let last = first
    let seeksOnActive = first.seeks.length
    for (let i = 0; i < 400 && engine.playing; i++) {
      step(engine, made, 1 / 30)
      const cur = engine.frameSource as FakeMedia
      if (cur !== last) {
        swaps++
        last = cur
        // the newly active element must already sit on the target time (no late seek)
        expect(cur.currentTime).toBeGreaterThanOrEqual(15 - 0.001)
        expect(cur.currentTime).toBeLessThan(15.2)
        seeksOnActive = cur.seeks.length
      }
      if (swaps === 1 && cur === last && cur !== first) break
    }
    expect(swaps).toBe(1)
    expect(first.paused).toBe(true)
    expect(seeksOnActive).toBe(1) // only the pre-seek
    expect(engine.time).toBeGreaterThanOrEqual(4)
    expect(engine.time).toBeLessThan(4.5)
  })

  it('plays through every range in order and ends at the programme duration', () => {
    const { engine, made } = setup()
    engine.play()
    const sources: number[] = []
    for (let i = 0; i < 1000 && engine.playing; i++) {
      step(engine, made, 1 / 30)
      sources.push(Math.round(engine.time))
    }
    expect(engine.playing).toBe(false)
    expect(engine.time).toBe(14)
    expect(engine.ended).toBe(true)
    // monotonic programme time
    for (let i = 1; i < sources.length; i++) expect(sources[i]).toBeGreaterThanOrEqual(sources[i - 1])
    expect(made.every((m) => m.paused)).toBe(true)
  })

  it('programme time stays continuous across boundaries (no jump larger than a frame + lead)', () => {
    const { engine, made } = setup()
    engine.play()
    let prev = engine.time
    let worst = 0
    while (engine.playing) {
      step(engine, made, 1 / 30)
      worst = Math.max(worst, engine.time - prev)
      prev = engine.time
    }
    expect(worst).toBeLessThan(1 / 30 + 0.05)
  })

  it('restarts from the beginning when played at the end', () => {
    const { engine } = setup()
    engine.seek(14)
    engine.play()
    expect(engine.time).toBe(0)
    expect(engine.playing).toBe(true)
  })

  it('seeking while playing keeps playing from the new place', () => {
    const { engine } = setup()
    engine.play()
    engine.seek(10)
    expect(engine.playing).toBe(true)
    expect(engine.frameSource!.currentTime).toBeCloseTo(31)
    expect(engine.frameSource!.paused).toBe(false)
  })

  it('setRanges after an edit keeps programme time and re-targets media', () => {
    const { engine } = setup()
    engine.seek(2)
    const tl = baseTimeline()
    tl.segments[0].cuts = []
    engine.setRanges(programMap(tl))
    expect(engine.duration).toBe(15)
    expect(engine.time).toBe(2)
    expect(engine.frameSource!.currentTime).toBe(12)
  })

  it('contiguous ranges of one source continue without a swap', () => {
    const made: FakeMedia[] = []
    const engine = new PlaybackEngine({
      createMedia: (id) => {
        const m = new FakeMedia(id)
        made.push(m)
        return m
      },
    })
    const tl = baseTimeline()
    tl.segments = [
      { id: 'a', source_id: 'a', start: 0, end: 2, cuts: [] },
      { id: 'b', source_id: 'a', start: 2, end: 4, cuts: [] },
    ]
    engine.setRanges(programMap(tl))
    engine.play()
    const first = engine.frameSource
    for (let i = 0; i < 200 && engine.playing; i++) step(engine, made, 1 / 30)
    expect(first!.paused).toBe(true)
    expect(made.filter((m) => m.plays > 0)).toHaveLength(1)
  })
})
