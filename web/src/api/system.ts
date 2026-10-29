// Typed client for the non-studio endpoints: settings, health, model gateway and system telemetry.
import { ApiError } from './errors'

export type KeyProvider = 'gemini' | 'anthropic' | 'openai' | 'nvidia' | 'openrouter' | 'groq' | 'deepseek'
export type KeyOrigin = 'keychain' | 'environment' | 'env_file' | 'none'

export interface Settings {
  vertex_configured?: boolean
  gemini_configured: boolean
  anthropic_configured: boolean
  openai_configured: boolean
  nvidia_configured?: boolean
  openrouter_configured?: boolean
  groq_configured: boolean
  deepseek_configured: boolean
  gemini_model: string
  credential_storage: Partial<Record<KeyProvider, KeyOrigin>>
  credential_warning: string | null
}

export interface Preset {
  id: string
  name: string
  description: string
  providers: string[]
}

export interface LocalModel {
  label: string
  installed: boolean
  ready: boolean
  path: string
  command: string
}

export interface Health extends Settings {
  version: string
  ffmpeg: boolean
  ffprobe: boolean
  mlx_available: boolean
  caption_burn_available: boolean
  local_models: Record<'whisper' | 'embeddings' | 'vision' | 'editor', LocalModel>
}

export interface Route {
  provider: string
  model: string
  options?: Record<string, unknown>
}
export interface Routing {
  defaults: Record<string, Route>
  fallbacks: Record<string, Route[]>
}
export interface Catalog {
  stages: string[]
  providers: { id: string; label: string }[]
  models: Record<string, string[]>
}

export interface ModelGroup {
  provider: string
  model: string
  stage: string
  calls: number
  failures: number
  p50_latency_ms: number | null
  p95_latency_ms: number | null
  input_tokens: number
  output_tokens: number
  thinking_tokens: number
  cached_tokens: number
  cost_usd: number | null
  cost_unknown_calls: number
}
export interface ModelSummary {
  prices_as_of: string | null
  groups: ModelGroup[]
}

export interface ModelCall {
  call_id: string
  created_at: number
  provider: string
  model: string
  stage: string
  status: string
  error_category: string | null
  input_tokens: number | null
  output_tokens: number | null
  thinking_tokens: number | null
  cost_usd: number | null
  latency_ms: number | null
  collection_id: string | null
  finish_reason: string | null
}
export interface ModelCallDetail extends ModelCall {
  request: Record<string, unknown> | null
  response: Record<string, unknown> | null
}

export interface RunRow {
  id: string
  workflow: string
  status: string
  observation: string
  created_ns: number
  ended_ns: number | null
  attributes: Record<string, unknown>
}
export interface SpanRow {
  id: string
  parent_id: string | null
  name: string
  status: string
  start_ns: number
  end_ns: number | null
  attributes: Record<string, unknown>
}
export interface SystemSummary {
  window_days: number
  attempts: number
  terminal_attempts: number
  failures: number
  interruptions: number
  time_to_ready_p50_seconds: number | null
  performance: {
    workflow: string
    cache_state: string
    attempts: number
    successes: number
    failed: number
    p50_seconds: number | null
    p95_seconds: number | null
  }[]
  recent_runs: RunRow[]
  diagnostics: { pending: number; last_error: string | null; writer_alive: boolean; dropped?: number }
  scope: string
}
export interface RunDetail extends RunRow {
  spans: SpanRow[]
  events: { name: string; at_ns: number; attributes: Record<string, unknown> }[]
}

async function call<T>(method: string, path: string, body?: unknown): Promise<T> {
  let res: Response
  try {
    res = await fetch(path, {
      method,
      headers: body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    })
  } catch {
    throw new ApiError(0, 'Cannot reach the Clippie server. Is it running?')
  }
  if (!res.ok) {
    let detail = res.statusText
    try {
      const j = await res.json()
      detail = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail)
    } catch {
      /* not json */
    }
    throw new ApiError(res.status, detail)
  }
  return (await res.json()) as T
}

export const systemApi = {
  health: () => call<Health>('GET', '/api/health'),
  saveKey: (provider: KeyProvider, value: string) => call<Settings>('POST', '/api/settings', { [`${provider}_key`]: value }),
  removeKey: (provider: KeyProvider) => call<Settings>('DELETE', `/api/settings/${provider}`),
  routing: () => call<Routing>('GET', '/api/models/routing'),
  saveRouting: (value: Routing) => call<Routing>('PUT', '/api/models/routing', value),
  catalog: () => call<Catalog>('GET', '/api/models/catalog'),
  available: (provider: string) => call<{ provider: string; models: string[] }>('GET', `/api/models/available/${encodeURIComponent(provider)}`),
  presets: () => call<{ presets: Preset[] }>('GET', '/api/models/presets'),
  applyPreset: (id: string) => call<{ routing: Routing; notes: string[] }>('POST', `/api/models/routing/preset/${encodeURIComponent(id)}`),
  modelSummary: () => call<ModelSummary>('GET', '/api/models/summary'),
  modelCalls: (filters: { stage?: string; provider?: string; status?: string }, limit = 100) => {
    const q = new URLSearchParams({ limit: String(limit) })
    for (const [k, v] of Object.entries(filters)) if (v) q.set(k, v)
    return call<{ calls: ModelCall[] }>('GET', `/api/models/calls?${q}`).then((r) => r.calls)
  },
  modelCall: (id: string) => call<ModelCallDetail>('GET', `/api/models/calls/${encodeURIComponent(id)}`),
  systemSummary: (observation: string) => call<SystemSummary>('GET', `/api/system/summary?observation=${observation}`),
  systemRun: (id: string) => call<RunDetail>('GET', `/api/system/runs/${encodeURIComponent(id)}`),
}

// ---- pure helpers (unit tested) --------------------------------------------------------------

export const KEY_PROVIDERS: { id: KeyProvider; label: string; use: string; link?: string }[] = [
  { id: 'gemini', label: 'Gemini (AI Studio)', use: 'Optional Developer API route. Google Cloud trial credits do not cover this endpoint. Transcript text and sampled pictures go to Google.', link: 'https://aistudio.google.com/api-keys' },
  { id: 'anthropic', label: 'Anthropic', use: 'Optional. Route any stage to Claude. Transcript text goes to Anthropic.' },
  { id: 'openai', label: 'OpenAI', use: 'Optional. Route any stage to OpenAI. Transcript text goes to OpenAI.' },
  { id: 'nvidia', label: 'NVIDIA NIM', use: 'Optional. Free developer tier for testing (Nemotron, gpt-oss). Transcript text goes to NVIDIA, which logs prompts.', link: 'https://build.nvidia.com/settings/api-keys' },
  { id: 'openrouter', label: 'OpenRouter', use: 'Optional. Many models behind one key, including free :free models. Transcript text goes to the chosen model host.', link: 'https://openrouter.ai/settings/keys' },
  { id: 'groq', label: 'Groq', use: 'Optional cloud transcription, and a fast chat provider for small review calls. Audio or transcript text goes to Groq.' },
  { id: 'deepseek', label: 'DeepSeek', use: 'Optional clip selection on source intake. Transcript text goes to DeepSeek.' },
]

export function configured(settings: Settings, provider: KeyProvider): boolean {
  return Boolean(settings[`${provider}_configured` as const])
}

export function originLabel(origin: KeyOrigin | undefined, isConfigured: boolean): string {
  if (!isConfigured) return 'Not set'
  if (origin === 'environment') return 'Set by environment variable'
  if (origin === 'env_file') return 'Set in private .env file'
  return 'Saved in Keychain'
}

export const externallyManaged = (origin: KeyOrigin | undefined) => origin === 'environment' || origin === 'env_file'

/** Provider ids that need a key; the local editor does not. */
export function routeNeedsKey(provider: string): KeyProvider | null {
  return ['gemini', 'anthropic', 'openai', 'nvidia', 'openrouter', 'groq'].includes(provider) ? (provider as KeyProvider) : null
}

export function routingChanged(a: Routing, b: Routing): boolean {
  return JSON.stringify(a.defaults) !== JSON.stringify(b.defaults)
}

export function fmtCost(usd: number | null | undefined, unknownCalls = 0): string {
  if (usd === null || usd === undefined) return 'unknown'
  const text = usd < 0.01 && usd > 0 ? `$${usd.toFixed(4)}` : `$${usd.toFixed(2)}`
  return unknownCalls > 0 ? `${text} + ${unknownCalls} unknown` : text
}

export function fmtMs(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return '—'
  return ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(ms < 10000 ? 2 : 1)} s`
}

export function fmtSeconds(s: number | null | undefined): string {
  if (s === null || s === undefined) return '—'
  return s < 60 ? `${s.toFixed(1)} s` : `${Math.floor(s / 60)} m ${Math.round(s % 60)} s`
}

export function fmtTokens(n: number | null | undefined): string {
  if (n === null || n === undefined) return '—'
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`
  if (n >= 10_000) return `${Math.round(n / 1000)}k`
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`
  return String(n)
}

export function title(text: string): string {
  const spaced = text.replaceAll('_', ' ')
  return spaced.charAt(0).toUpperCase() + spaced.slice(1)
}

export function totals(groups: ModelGroup[]) {
  const priced = groups.filter((g) => g.cost_usd !== null)
  return {
    calls: groups.reduce((n, g) => n + g.calls, 0),
    failures: groups.reduce((n, g) => n + g.failures, 0),
    cost: priced.length ? priced.reduce((n, g) => n + (g.cost_usd ?? 0), 0) : null,
    unknownCalls: groups.reduce((n, g) => n + g.cost_unknown_calls, 0),
    tokens: groups.reduce((n, g) => n + g.input_tokens + g.output_tokens + g.thinking_tokens, 0),
  }
}
