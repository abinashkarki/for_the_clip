// Seed data for the mock layer: a scripted transcript timed against public/mock/source.webm (40 s, 640x360).
import type { SourceWord } from '../render/types'
import type { CaptionTemplate, HookStyle, TemplateRegistry } from '../render/types'

export const MOCK_SOURCE_ID = 'job-945'
export const MOCK_SOURCE_DURATION = 52
export const MOCK_SOURCE = {
  title: 'Sal Khan on Transforming Education',
  duration: MOCK_SOURCE_DURATION,
  media_url: 'mock/source.webm',
  width: 640,
  height: 360,
}

const SCRIPT = [
  'So how would you approach a problem like this?',
  'Most tutors would just give you the answer, but um this one refuses.',
  "It asks you a question back. What have you tried so far?",
  'And honestly, that changes everything, because students stop copying and start thinking.',
  'You know, the goal was never faster homework.',
  'It was building a patient teacher for every kid on earth.',
  'Uh, when we tested it in classrooms, the results surprised us.',
  'Kids who struggled the most improved the fastest.',
  "The secret is simple. Don't hand over the answer, guide the student toward it, one small step at a time.",
  'That is how real learning works, and now it can scale to millions of people.',
]

/** extra silence (s) after a sentence, so the silence remover has something to find */
const PAUSES: Record<number, number> = { 3: 0.75, 5: 0.9, 7: 0.7 }

export interface Sentence {
  index: number
  words: SourceWord[]
  start: number
  end: number
}

function buildTranscript(): { words: SourceWord[]; sentences: Sentence[] } {
  const words: SourceWord[] = []
  const sentences: Sentence[] = []
  let t = 1.0
  let id = 1200
  SCRIPT.forEach((line, si) => {
    const parts = line.split(/\s+/)
    const sw: SourceWord[] = []
    parts.forEach((raw) => {
      const clean = raw.replace(/[^\w']/g, '')
      const dur = Math.min(0.5, 0.14 + 0.035 * clean.length)
      const w: SourceWord = {
        key: `${MOCK_SOURCE_ID}:${id}`,
        source_id: MOCK_SOURCE_ID,
        word_id: id,
        text: raw,
        start: Math.round(t * 1000) / 1000,
        end: Math.round((t + dur) * 1000) / 1000,
      }
      id++
      t += dur + 0.03
      if (/[.?!]$/.test(raw)) t += 0.42 + (PAUSES[si] ?? 0)
      else if (/,$/.test(raw)) t += 0.18
      sw.push(w)
      words.push(w)
    })
    sentences.push({ index: si, words: sw, start: sw[0].start, end: sw[sw.length - 1].end })
  })
  return { words, sentences }
}

export const TRANSCRIPT = buildTranscript()

export const FILLER_WORDS = new Set(['um', 'uh', 'hmm'])

// Stream B's real templates (clippie/templates/{captions,hooks}). Metrics are the derived ascent/descent the server adds.
import boldPop from './templates/bold_pop.json'
import boxedWord from './templates/boxed_word.json'
import colorWord from './templates/color_word.json'
import impactOne from './templates/impact_one.json'
import karaoke from './templates/karaoke.json'
import minimalLower from './templates/minimal_lower.json'
import phraseClean from './templates/phrase_clean.json'
import podcastBox from './templates/podcast_box.json'
import hookBanner from './templates/hook_banner.json'
import hookBox from './templates/hook_box.json'
import hookOutline from './templates/hook_outline.json'

const METRICS: Record<string, { ascent: number; descent: number }> = {
  'Poppins-SemiBold.ttf': { ascent: 1.05, descent: 0.35 },
  'Poppins-Bold.ttf': { ascent: 1.05, descent: 0.35 },
  'Inter-SemiBold.ttf': { ascent: 0.97, descent: 0.24 },
  'Inter-Bold.ttf': { ascent: 0.97, descent: 0.24 },
  'Inter-Medium.ttf': { ascent: 0.97, descent: 0.24 },
  'Montserrat-ExtraBold.ttf': { ascent: 0.97, descent: 0.25 },
  'Anton-Regular.ttf': { ascent: 1.18, descent: 0.33 },
}
const withMetrics = <T extends { font: { file: string } }>(t: T): T => ({
  ...t,
  font: { ...t.font, metrics: METRICS[t.font.file] },
})

export const CAPTION_TEMPLATES = [boldPop, karaoke, impactOne, boxedWord, colorWord, phraseClean, minimalLower, podcastBox].map(
  (t) => withMetrics(t),
) as unknown as CaptionTemplate[]

export const HOOK_STYLES = [hookBox, hookOutline, hookBanner].map((t) => withMetrics(t)) as unknown as HookStyle[]

export const MOCK_REGISTRY: TemplateRegistry = {
  captions: CAPTION_TEMPLATES,
  hooks: HOOK_STYLES,
  layouts: ['fill', 'fit', 'stack', 'screen_speaker'],
  fonts_base_url: '/static/fonts/',
}
