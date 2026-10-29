// Timeline v3 types (mirror of docs/v3/TIMELINE_V3.md)

export type CutReason = 'filler' | 'silence' | 'manual'
export interface Cut {
  start: number
  end: number
  reason: CutReason
}
export interface Segment {
  id: string
  source_id: string
  start: number
  end: number
  cuts: Cut[]
}
export interface Keyframe {
  t: number
  x: number
  y: number
  zoom: number
}
export interface Region {
  keyframes: Keyframe[]
}
export type LayoutKind = 'fill' | 'fit' | 'stack' | 'screen_speaker'
export interface Shot {
  id: string
  start: number
  end: number
  layout: LayoutKind
  regions: Region[]
}
export interface Broll {
  id: string
  source_id: string
  source_start: number
  at: number
  duration: number
  framing: { x: number; y: number; zoom: number }
}
export interface CaptionOverrides {
  font?: { family?: string; file?: string; size?: number }
  case?: CaseMode
  position?: { y?: number }
  colors?: Partial<CaptionColors>
  stroke_width?: number
  active_word?: { style?: ActiveStyle }
  animation?: { in?: AnimationIn }
  grouping?: { max_words?: number }
}
export interface CaptionsSpec {
  enabled: boolean
  template_id: string
  overrides: CaptionOverrides
  emphasis: string[]
  hidden: string[]
}
export interface HookOverlay {
  id: string
  kind: 'hook'
  text: string
  start: number
  end: number
  style: string
}
export interface ProgressBarOverlay {
  id: string
  kind: 'progress_bar'
  color: string
  position: 'top' | 'bottom'
  thickness: number
}
export type LogoAnchor = 'top_left' | 'top_right' | 'bottom_left' | 'bottom_right'
export interface LogoOverlay {
  id: string
  kind: 'logo'
  asset_id: string
  anchor: LogoAnchor
  scale: number
  opacity: number
}
export type Overlay = HookOverlay | ProgressBarOverlay | LogoOverlay

export interface CanvasSpec {
  width: number
  height: number
  fps: number
  background: { kind: 'blur' | 'color'; color: string }
}
export interface Timeline {
  schema_version: 3
  canvas: CanvasSpec
  segments: Segment[]
  shots: Shot[]
  broll: Broll[]
  captions: CaptionsSpec
  overlays: Overlay[]
  brand_kit_id: string | null
  audio: { normalize: boolean }
}

export interface ProgramRange {
  source_id: string
  src_start: number
  src_end: number
  at: number
  segment_id?: string
}

export interface SourceWord {
  key: string
  source_id: string
  word_id: number
  text: string
  start: number
  end: number
}

export interface ProgramWord {
  key: string
  text: string
  start: number
  end: number
  source_id: string
  word_id: number
  emphasis: boolean
  hidden: boolean
  /** index of the program_map range the word falls in */
  range: number
}

// ---- caption templates (docs/v3/CAPTION_TEMPLATES.md)
export type CaseMode = 'none' | 'upper' | 'lower'
export type ActiveStyle = 'none' | 'color' | 'box' | 'scale' | 'color_box'
export type AnimationIn = 'none' | 'fade' | 'pop' | 'slide_up'
export interface CaptionColors {
  text: string
  active: string
  emphasis: string
  stroke: string
  box: string | null
  active_box: string | null
}
/** ascent/descent in em, derived by the server from the font file */
export interface FontMetrics {
  ascent: number
  descent: number
}
export interface CaptionTemplate {
  id: string
  name: string
  font: { family: string; file: string; size: number; metrics?: FontMetrics }
  case: CaseMode
  grouping: { max_words: number; max_chars: number; max_lines: number; break_on_pause: number }
  position: { y: number; max_width: number }
  colors: CaptionColors
  stroke_width: number
  shadow: { dx: number; dy: number; blur: number; color: string }
  active_word: { style: ActiveStyle; scale: number }
  animation: { in: AnimationIn; duration_ms: number }
  line_height: number
}
export interface HookStyle {
  id: string
  name?: string
  font: { family: string; file: string; metrics?: FontMetrics }
  size: number
  case: CaseMode
  colors: { text: string; box: string | null; stroke: string | null }
  stroke_width: number
  shadow: { dx: number; dy: number; blur: number; color: string }
  position: { y: number }
  max_width: number
  padding: number
  radius: number
  line_height: number
  /** full-width band (hook_banner) */
  band?: boolean
  animation: { in: AnimationIn; duration_ms: number }
}
export interface TemplateRegistry {
  captions: CaptionTemplate[]
  hooks: HookStyle[]
  layouts: LayoutKind[]
  fonts_base_url: string
}
