// Typed client for /api/publish. With ?mock=1 (or when the studio API fell back to mock data) it uses publishMock.
import { ApiError } from './errors'
import { isMock } from './client'
import { mockPublish } from './publishMock'
import type { Check, RetentionPoint, RetentionWord } from './publishLogic'

export type { Check, RetentionPoint, RetentionWord }

export interface Channel {
  id: string
  platform: 'youtube' | 'tiktok' | 'instagram'
  provider: 'youtube' | 'aggregator'
  display_name: string
  external_id: string | null
  scopes: string[]
  created_at: string
}

export interface PublishSettings {
  youtube_client_id: string
  youtube_client_secret_set: boolean
  aggregator_key_set: boolean
  aggregator_base_url: string
  cloud_url: string
  cloud_device_token_set: boolean
  redirect_uri_hint: string
}

export type SettingsPatch = Partial<{
  youtube_client_id: string
  youtube_client_secret: string
  aggregator_key: string
  aggregator_base_url: string
  cloud_url: string
  cloud_device_token: string
}>

export type PostStatus = 'draft' | 'scheduled' | 'uploading' | 'published' | 'failed'

export interface Metrics {
  views?: number
  likes?: number
  comments?: number
  shares?: number
  saves?: number
  reach?: number
  subscribers_gained?: number
  watch_minutes?: number
  avg_view_duration_s?: number
  avg_view_percentage?: number
  skip_rate?: number
}

export interface Post {
  id: string
  platform: Channel['platform']
  channel_id: string
  channel_name: string | null
  short_id: string
  revision_id: string
  artifact_id: string | null
  title: string
  description: string
  hashtags: string[]
  status: PostStatus
  scheduled_at: string | null
  published_at: string | null
  created_at: string
  external_id: string | null
  url: string | null
  error: string | null
  warnings: string[]
  duration: number | null
  rendering: boolean
  export_recovered?: boolean
  export_preview_url?: string | null
  metrics: Metrics | null
  metrics_at: string | null
  project_id?: string | null
  project_name?: string | null
  project_deleted?: boolean
}

export interface CreatePostsInput {
  short_id: string
  revision_id: string
  platforms: string[]
  title: string
  description: string
  hashtags: string[]
  scheduled_at?: string | null
  draft?: boolean
}

export interface RetentionData {
  post_id: string
  revision_id: string
  fetched_at: string | null
  points: RetentionPoint[]
  duration: number | null
  words: RetentionWord[]
  drops: number[]
  /** Which viewers the curve covers: organic, all, or not_available_yet when YouTube has none yet. */
  audience?: 'organic' | 'all' | 'not_available_yet' | null
}

export interface Performance {
  short_id: string
  current_revision_id: string | null
  posts: Post[]
  totals: Record<string, number>
  revisions: { revision_id: string; posts: number; views: number }[]
}

export interface MetricsHistory {
  post: Post
  latest: { fetched_at: string; source: string; metrics: Metrics } | null
  history: { fetched_at: string; source: string; metrics: Metrics }[]
}

export interface YoutubeStart {
  authorization_url: string
  state: string
  redirect_uri: string
  mock?: boolean
}

export interface PublishApi {
  settings(): Promise<PublishSettings>
  saveSettings(patch: SettingsPatch): Promise<PublishSettings>
  channels(): Promise<Channel[]>
  connectYoutube(): Promise<YoutubeStart>
  connectAggregator(apiKey?: string): Promise<Channel[]>
  disconnect(id: string): Promise<void>
  createPosts(input: CreatePostsInput): Promise<Post[]>
  listPosts(shortId?: string): Promise<Post[]>
  publishPost(id: string): Promise<Post>
  retryPost(id: string): Promise<Post>
  resolveUncertain(id: string): Promise<Post>
  deletePost(id: string): Promise<void>
  syncPost(id: string): Promise<Post>
  metrics(id: string): Promise<MetricsHistory>
  retention(id: string): Promise<RetentionData>
  performance(shortId: string): Promise<Performance>
  cloudSync(): Promise<{ posts_registered: number; snapshots_merged: number }>
}

const BASE = '/api/publish'

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  let res: Response
  try {
    res = await fetch(BASE + path, {
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
  if (res.status === 204) return undefined as T
  return (await res.json()) as T
}

const id = encodeURIComponent

const real: PublishApi = {
  settings: () => request('GET', '/settings'),
  saveSettings: (patch) => request('PUT', '/settings', patch),
  channels: async () => (await request<{ channels: Channel[] }>('GET', '/channels')).channels,
  connectYoutube: () => request('POST', '/channels/youtube/connect'),
  connectAggregator: async (apiKey) => (await request<{ channels: Channel[] }>('POST', '/channels/aggregator/connect', apiKey ? { api_key: apiKey } : {})).channels,
  disconnect: (cid) => request('DELETE', `/channels/${id(cid)}`),
  createPosts: async (input) => (await request<{ posts: Post[] }>('POST', '/posts', input)).posts,
  listPosts: async (shortId) => (await request<{ posts: Post[] }>('GET', shortId ? `/posts?short_id=${id(shortId)}` : '/posts')).posts,
  publishPost: (pid) => request('POST', `/posts/${id(pid)}/publish`, {}),
  retryPost: (pid) => request('POST', `/posts/${id(pid)}/retry`),
  resolveUncertain: (pid) => request('POST', `/posts/${id(pid)}/resolve-uncertain`, { checked_not_uploaded: true }),
  deletePost: (pid) => request('DELETE', `/posts/${id(pid)}`),
  syncPost: (pid) => request('POST', `/posts/${id(pid)}/sync`),
  metrics: (pid) => request('GET', `/posts/${id(pid)}/metrics`),
  retention: (pid) => request('GET', `/posts/${id(pid)}/retention`),
  performance: (sid) => request('GET', `/clips/${id(sid)}/performance`),
  cloudSync: () => request('POST', '/cloud/sync'),
}

export const publishApi: PublishApi = new Proxy(real, {
  get: (target, key: string) => (...args: never[]) => ((isMock() ? mockPublish : target) as unknown as Record<string, (...a: never[]) => unknown>)[key](...args),
}) as PublishApi
