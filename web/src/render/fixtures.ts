import type { SourceWord, Timeline } from './types'

export function baseTimeline(over: Partial<Timeline> = {}): Timeline {
  return {
    schema_version: 3,
    canvas: { width: 1080, height: 1920, fps: 30, background: { kind: 'blur', color: '#000000' } },
    segments: [
      { id: 's1', source_id: 'a', start: 10, end: 20, cuts: [{ start: 14, end: 15, reason: 'filler' }] },
      { id: 's2', source_id: 'a', start: 30, end: 35, cuts: [] },
    ],
    shots: [],
    broll: [],
    captions: { enabled: true, template_id: 'bold_pop', overrides: {}, emphasis: [], hidden: [] },
    overlays: [],
    brand_kit_id: null,
    audio: { normalize: true },
    ...over,
  }
}

/** One word per 0.5 s from t0..t1 in source a. */
export function wordsBetween(t0: number, t1: number, sourceId = 'a', idStart = 0): SourceWord[] {
  const out: SourceWord[] = []
  let id = idStart
  for (let t = t0; t + 0.5 <= t1 + 1e-9; t += 0.5) {
    out.push({ key: `${sourceId}:${id}`, source_id: sourceId, word_id: id, text: `w${id}`, start: t, end: t + 0.5 })
    id++
  }
  return out
}
